"""Standalone, passive Wailing Wilma visual warning skill."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import sqlite3
import threading
import time
from concurrent.futures import Future
from typing import NamedTuple
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from api.enums import SpeechPriority
from services.image_generation import vision_base64
from skills.skill_base import Skill, tool

from .feedback import (
    FEEDBACK_ISSUES,
    FeedbackCapture,
    FeedbackNotice,
    WarningSubmission,
    record_feedback,
    record_missed_warning,
    utc_timestamp,
)
from .foreground import foreground_game
from .image_encoding import encode_capture
from .policy import DEFAULT_CONFIRMATIONS, VEHICLE_SCENES, WilmaPolicy, fold_text, parse_visual_report
from .preferences import read_preferences, read_reader_settings
from .prompt import build_prompt
from .reader import ReaderSettings, ReaderUnavailable, read_image

_PROMPT = build_prompt()

# Game-log warnings that Wingman's stock Star Citizen Events skill already
# speaks, mapped to the Wilma category it covers, with that skill's switches.
_NOT_PERCENT = frozenset({"power", "heat", "environment", "emp", "fire", "hydration", "nutrition",
                          "blood_loss", "pressure", "incapacitation"})
_SPOKEN = {"quantum_fuel": "quantum fuel", "life_support": "oxygen", "emp": "EMP",
           "suit_oxygen": "suit oxygen", "hydration": "hydration", "nutrition": "nutrition",
           "blood_loss": "blood loss", "pressure": "high pressure"}
_FIRST_PROBLEM_TIMEOUT_S = 3
_STOP_TIMEOUT_S = 5
_OWNED_TIMEOUT = object()
_START_FAILED = object()
# Words a player may use for a category, mapped to Wilma's category names.
_HEARD = {
    "shield": "shields", "shields": "shields", "component": "components",
    "components": "components", "system": "components", "systems": "components",
    "fuel": "fuel", "hydrogen": "fuel", "hydrogen_fuel": "fuel",
    "quantum": "quantum_fuel", "quantum_fuel": "quantum_fuel",
    "oxygen": "life_support", "life_support": "life_support", "hull": "hull",
    "suit_oxygen": "suit_oxygen", "helmet_oxygen": "suit_oxygen", "personal_oxygen": "suit_oxygen",
    "armor": "armor", "armour": "armor", "power": "power", "heat": "heat",
    "overheating": "heat", "temperature": "heat", "environment": "environment",
    "emp": "emp", "fire": "fire", "fires": "fire",
    "hydration": "hydration", "nutrition": "nutrition",
    "blood_loss": "blood_loss", "bleeding": "blood_loss",
    "pressure": "pressure", "high_pressure": "pressure",
    "incapacitation": "incapacitation", "incapacitated": "incapacitation",
}

_SCENE_PLAIN = {
    "fps": "on foot", "menu_or_map": "a menu or map is showing",
    "cutscene_or_loading": "a cutscene or loading screen", "unknown": "scene not recognised",
}

# Plain words for the foreground source's status codes, for the player.
_PLAIN = {
    "star_citizen_not_foreground": "waiting for Star Citizen to be the window in front",
    "foreground_source_not_supported_on_this_platform": "screen reading needs Windows",
    "foreground_geometry_unavailable": "cannot find the Star Citizen window area",
    "foreground_source_unavailable": "cannot check which window is in front",
    "video_player_not_foreground":
        "test mode, waiting for a Chrome or Firefox video window to be in front",
    "screen_source_unknown": "the screen source setting is not recognised",
}


_ENGINEERING_REVIEW_ACTIONS = {
    "front_shields": "v_shield_raise_level_forward",
    "rear_shields": "v_shield_raise_level_back",
    "left_shields": "v_shield_raise_level_left",
    "right_shields": "v_shield_raise_level_right",
    "top_shields": "v_shield_raise_level_up",
    "bottom_shields": "v_shield_raise_level_down",
    "weapons_power_down": "v_engineering_assignment_weapons_decrease",
    "weapons_power_up": "v_engineering_assignment_weapons_increase",
    "shield_power_up": "v_engineering_assignment_shields_increase",
    "shield_power_down": "v_engineering_assignment_shields_decrease",
    "engines_power_up": "v_engineering_assignment_engine_increase",
    "engines_power_down": "v_engineering_assignment_engine_decrease",
    "reset_power": "v_engineering_assignment_reset",
    "reset_shields": "v_shield_reset_level",
}
_SPOKEN_ENGINEERING_REVIEW = "sc_review_engineering_change"


def _owner_review_link(page: object, action_id: str) -> str | None:
    if not isinstance(page, str) or any(not "!" <= c <= "~" for c in page):
        return None
    try:
        parts = urlsplit(page)
        host = ipaddress.ip_address(parts.hostname or "")
        port = parts.port
    except ValueError:
        return None
    if (not host.is_loopback or parts.scheme not in {"http", "https"} or port is None
            or parts.path not in {"/computer/", "/computer/index.html"}
            or parts.username is not None or parts.query or parts.fragment
            or "%" in parts.netloc
            or "?" in page or "#" in page):
        return None
    host_part = f"[{host}]" if host.version == 6 else str(host)
    query = urlencode({"engineering_action": action_id})
    return f"{parts.scheme}://{host_part}:{port}/computer/?{query}"


# Spoken once when the offloaded reader drops, and once when it comes back (A-17).
READER_DOWN_LINE = "Wilma can't reach its reader, warnings paused."
READER_BACK_LINE = "Wilma's reader is back, warnings resumed."


def _spoken(names):
    return ", ".join(_SPOKEN.get(name, name) for name in names)


class _CaptureContext(NamedTuple):
    fingerprint: str | None
    off_seat: bool = False
    seated: bool = False


class WailingWilma(Skill):
    """Opt-in warnings using the configured Wingman vision and speech facades."""

    def __init__(self, config, settings, wingman):
        super().__init__(config=config, settings=settings, wingman=wingman)
        self._task = None
        self._thread = None
        self._worker_loop = None
        self._stopping = False
        self._unloading = False
        self._state_lock = threading.RLock()
        self._generation = 0
        self._muted = False
        self._reader_down = None  # None: no read yet this run
        self._policy = WilmaPolicy()
        self._status = "stopped"
        self._scope = None
        self._poll_interval = 1.0
        self._image_detail = "stock"
        self._using_stock_detail = False
        self._reader_signature = (ReaderSettings(), "")
        self._waterfall_route = None
        self._stock_subscription = None
        self._owned_tasks = set()
        self._last_warning_submission: WarningSubmission | None = None

    async def prepare(self):
        await super().prepare()
        preferences = read_preferences(
            lambda name: self.retrieve_custom_property_value(name, []))
        self._poll_interval = preferences.interval_seconds
        self._image_detail = preferences.image_detail
        self._using_stock_detail = False
        self._reader_signature = (preferences.reader, preferences.reader_secret)
        self._policy = preferences.warning_policy()
        if self.retrieve_custom_property_value("start_enabled", []) is True:
            await self.wilma_start()

    async def unload(self):
        with self._state_lock:
            self._unloading = True
        await self.wilma_stop()
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(
                "Wailing Wilma is still settling owned work; unload is not complete.")
        await super().unload()

    @tool(name="wilma_start", description="Start passive Wailing Wilma visual warnings.")
    async def wilma_start(self) -> str:
        with self._state_lock:
            if self._unloading or self.is_unloaded:
                return "Wailing Wilma is unloaded; enable the skill again before starting it."
            if self._thread is not None and self._thread.is_alive():
                return "Wailing Wilma is already running or stopping."
            self._generation += 1
            self._reset_policy()
            self._last_warning_submission = None
            self._reader_down = None
            self._using_stock_detail = False
            self._status = "starting"
            self._stopping = False
            generation = self._generation
            startup = Future()
            self._follow_stock_events()
            self._thread = self.wingman.run_in_thread(self._worker, generation, startup)
            if self._thread is None:
                self._unfollow_stock_events()
                self._status = "worker could not start"
                return "Wailing Wilma could not start its visual worker."
        problem = await asyncio.shield(asyncio.wrap_future(startup))
        if generation != self._generation:
            return "Wailing Wilma was stopped."
        if problem is _START_FAILED:
            return "Wailing Wilma could not start its visual worker."
        if problem:
            return f"Wailing Wilma started{problem}."
        return ("Wailing Wilma started. Visual warnings are estimates; ask for "
                "Wailing Wilma status to hear what it is reading.")

    async def _worker(self, generation, startup):
        """Own the loop past the initiating assistant turn, including slow child cleanup."""
        with self._state_lock:
            self._worker_loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        try:
            if generation != self._generation:
                return
            problem = await self._first_problem()
            startup.set_result(problem)
            if generation == self._generation:
                await self._run(generation)
        except asyncio.CancelledError:
            pass  # the supplier's thread helper closes its loop after this coroutine settles
        except Exception:
            self.log.error("Wailing Wilma visual worker failed.", server_only=True)
        finally:
            if not startup.done():
                startup.set_result(_START_FAILED)
            # A bounded first-use probe may still be running. Keep its loop alive;
            # a stop timeout retains this worker and prohibits a replacement.
            if self._owned_tasks:
                draining = asyncio.gather(*tuple(self._owned_tasks), return_exceptions=True)
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    await draining
            with self._state_lock:
                self._reset_policy()
                self._scope = None
                self._worker_loop = self._task = None
                self._status = "stopped"

    async def _first_problem(self):
        """Name the first thing that will stop a new player hearing warnings.

        Checks only what is cheap and certain; an unknown answer is no problem.
        """
        try:
            reader, secret_name = self._read_reader()
            if reader.provider == "wingman":
                sees_images = getattr(self.wingman.ai, "sees_images", None)
                if (callable(sees_images)
                        and await self._owned(
                            sees_images(), _FIRST_PROBLEM_TIMEOUT_S, bounded=True) is False):
                    return (", but the configured model cannot read images; choose an "
                            "image-capable model or a separate vision reader in its settings")
            elif secret_name:
                has_secret = await self._owned(
                    self.wingman.secrets.retrieve(secret_name), _FIRST_PROBLEM_TIMEOUT_S,
                    bounded=True)
                if has_secret is not _OWNED_TIMEOUT and not has_secret:
                    return ", but the selected vision reader has no stored key in Wingman's Secrets"
        except Exception:
            pass
        window, reason = self._window()
        if window is None:
            return "; status: " + _PLAIN.get(reason, str(reason))
        return None

    @tool(name="wilma_stop", description="Stop passive Wailing Wilma warnings.")
    async def wilma_stop(self) -> str:
        with self._state_lock:
            thread = self._thread
            task, loop = self._task, self._worker_loop
            cancel = not self._stopping
            if cancel:
                self._generation += 1
                self._stopping = True
            stopped_generation = self._generation
            self._unfollow_stock_events()
        if cancel and loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # worker already finished and its loop closed
                pass
        if thread is not None:
            await asyncio.to_thread(thread.join, _STOP_TIMEOUT_S)
            if thread.is_alive():
                with self._state_lock:
                    self._status = "stopping; waiting for owned work to finish"
                return "Wailing Wilma is stopping; waiting for owned work to finish."
        with self._state_lock:
            if self._thread is thread and self._generation == stopped_generation:
                self._reset_policy()
                self._scope = None
                self._status = "stopped"
        return "Wailing Wilma stopped."

    @tool(name="wilma_status", description="Read Wailing Wilma's passive warning status.")
    async def wilma_status(self) -> str:
        with self._state_lock:
            return self._status_text()

    @tool(name="wilma_report_warning",
          description=("On an explicit player request, report Wilma's last warning as wrong, late "
                       "or repeated, or report a missed warning. For missed, put what the player "
                       "saw in note. For other issues, note is optional. Saves feedback locally."))
    async def wilma_report_warning(self, issue: str, note: str | None = None) -> str:
        if type(issue) is not str or issue not in FEEDBACK_ISSUES:
            return "Choose wrong, late, repeated, or missed."
        if note is not None and (type(note) is not str or len(note) > 300):
            return "Keep the warning correction to 300 characters or fewer."
        if issue == "missed":
            if not note or not note.strip():
                return "What did you see that Wilma should have announced?"
            with self._state_lock:
                running = bool(self._thread and self._thread.is_alive() and not self._stopping)
                muted = self._muted
                source = self._screen_source()
            try:
                await asyncio.to_thread(
                    record_missed_warning, self.get_generated_files_dir(),
                    note.strip(), utc_timestamp(),
                    monitor_running=running, muted=muted,
                    screen_source=source if source in ("game", "video_playback") else "unknown",
                )
            except (OSError, sqlite3.Error):
                return "Wilma could not save the warning report locally."
            return "Wilma saved the missed warning you described for review."
        with self._state_lock:
            warning = self._last_warning_submission
        if warning is None:
            return "Wilma has no confirmed warning submission from this watch to report."
        try:
            saved = await asyncio.to_thread(record_feedback, self.get_generated_files_dir(),
                                            warning, issue, (note or "").strip() or None)
        except (OSError, sqlite3.Error):
            return "Wilma could not save the warning report locally."
        return ("Wilma saved your warning report for review." if saved
                else "Wilma already has a report for that warning.")

    def _status_text(self):
        available = self._policy.availability(now_ms=time.monotonic_ns() // 1_000_000)
        reading = _spoken(available["reading"])
        status = _PLAIN.get(self._status, self._status)
        detail = "stock (high exceeded this reader's context)" if self._using_stock_detail else self._image_detail
        text = (f"Wailing Wilma: {status}; {'muted' if self._muted else 'unmuted'}; "
                f"image detail: {detail}; "
                f"reader: {self._reader_signature[0].provider}; "
                f"reading now: {reading or 'nothing'}")
        if self._waterfall_route:
            text += "; last Waterfall route: " + self._waterfall_route
        if available["muted"]:
            text += "; muted for now: " + _spoken(available["muted"])
        if self._policy.voice_low:
            text += "; warning point changed for now: " + ", ".join(
                f"{_SPOKEN.get(c, c)} {v:g}%" for c, v in sorted(self._policy.voice_low.items()))
        off_categories = tuple(available["off"]) + tuple(
            category for category in ("hydration", "nutrition", "blood_loss", "pressure", "incapacitation")
            if category not in self._policy.personal_status_categories
            and category not in available["off"]
        )
        if off_categories:
            text += "; off in settings: " + _spoken(off_categories)
        if self._policy.confirmations != DEFAULT_CONFIRMATIONS:
            text += f"; fresh readings needed for a warning: {self._policy.confirmations}"
        if self._screen_source() == "video_playback":
            text += "; test mode: reading a video window, not the game"
        return text + "."

    @tool(name="wilma_mute", description="Mute or unmute Wailing Wilma warnings.")
    async def wilma_mute(self, muted: bool) -> str:
        with self._state_lock:
            self._muted = muted
            if muted:
                self._policy.clear_warning_reference()
        return "Wailing Wilma muted." if muted else "Wailing Wilma unmuted."

    @tool(name="wilma_mute_category",
          description=("Mute or unmute one kind of Wailing Wilma warning until changed "
                       "again: shields, components, fuel, quantum fuel, oxygen, suit oxygen, hydration, nutrition, "
                       "blood loss, high pressure, incapacitation, hull, "
                       "armor, power, heat, environment, EMP or fire. "
                       "Saved settings are unchanged."))
    async def wilma_mute_category(self, category: str, muted: bool) -> str:
        heard = " ".join(str(category).lower().replace("-", " ").split())
        name = _HEARD.get(heard.removesuffix(" warnings").removesuffix(" warning")
                          .replace(" ", "_"))
        if name is None or type(muted) is not bool:
            return ("Wailing Wilma did not change anything. Say one of: shields, components, "
                    "fuel, quantum fuel, oxygen, suit oxygen, hydration, nutrition, blood loss, high pressure, incapacitation, "
                    "hull, armor, power, heat, environment, "
                    "EMP or fire.")
        with self._state_lock:
            if name == "suit_oxygen" and not muted and name not in self._policy.category_thresholds:
                return "Wailing Wilma suit oxygen warnings are still off in saved settings."
            if (name in {"hydration", "nutrition", "blood_loss", "pressure", "incapacitation"} and not muted
                    and name not in self._policy.personal_status_categories):
                return f"Wailing Wilma {_SPOKEN.get(name, name)} warnings are still off in saved settings."
            self._policy.mute_for_now(name, muted)
            spoken = _SPOKEN.get(name, name)
            if not muted:
                if name in self._policy.muted_categories:
                    return f"Wailing Wilma {spoken} warnings are still off in saved settings."
                return f"Wailing Wilma {spoken} warnings unmuted."
        return f"Wailing Wilma {spoken} warnings muted until you unmute them."

    @tool(name="wilma_warn_at",
          description=("Change the low-level warning point for one percentage warning "
                       "(shields, components, fuel, quantum fuel, oxygen, suit oxygen, hull or armor) "
                       "until reset or Wingman reloads. Percent 0 restores the saved level. "
                       "Saved settings are unchanged."))
    async def wilma_warn_at(self, category: str, percent: float) -> str:
        heard = " ".join(str(category).lower().replace("-", " ").split())
        name = _HEARD.get(heard.removesuffix(" warnings").removesuffix(" warning")
                          .replace(" ", "_"))
        if (name is None or name in _NOT_PERCENT
                or type(percent) not in (int, float)):
            return ("Wailing Wilma did not change anything. Say one of: shields, components, "
                    "fuel, quantum fuel, oxygen, suit oxygen, hull or armor, and a percentage.")
        spoken = _SPOKEN.get(name, name)
        try:
            with self._state_lock:
                if name == "suit_oxygen" and name not in self._policy.category_thresholds:
                    return "Wailing Wilma suit oxygen warnings are still off in saved settings."
                low, _critical = self._policy.set_low_for_now(
                    name, None if percent == 0 else percent)
        except ValueError:
            with self._state_lock:
                critical = self._policy.levels(name)[1]
            return (f"Wailing Wilma did not change anything. The {spoken} warning point "
                    f"must be between {critical:g} and 100 percent.")
        if percent == 0:
            return f"Wailing Wilma {spoken} warnings are back to {low:g} percent."
        return f"Wailing Wilma will warn about {spoken} at {low:g} percent until you reset it."

    @tool(name="wilma_review_adjustment",
          description=("For an explicit request for one engineering adjustment, start its "
                       "spoken confirmation review or show its owner review link. Actions: "
                       "front_shields, rear_shields, left_shields, "
                       "right_shields, top_shields, bottom_shields, weapons_power_down, "
                       "weapons_power_up, shield_power_up, shield_power_down, "
                       "engines_power_up, engines_power_down, reset_power, reset_shields. "
                       "Use last_alerted_shield "
                       "only when the player explicitly refers to the last spoken shield "
                       "warning. Clarify other unnamed targets. A warning alone is never an order. "
                       "The companion must receive physical player confirmation before it "
                       "can apply a spoken review."))
    async def wilma_review_adjustment(self, adjustment: str) -> str:
        if self._screen_source() == "video_playback":
            return "Engineering adjustments are unavailable in video test mode."
        if adjustment == "last_alerted_shield":
            window, _ = self._window()
            with self._state_lock:
                face = None
                if (window is not None and not self._muted
                        and self._scope == window.scope + ":" + str(self._context().fingerprint)
                        and self._read_reader() == self._reader_signature):
                    face = self._policy.last_alerted_shield(time.monotonic_ns() // 1_000_000)
                else:
                    self._policy.clear_warning_reference()
            if face is None:
                return "Please name the shield face you want to increase."
            face = {"port": "left", "starboard": "right"}.get(face, face)
            adjustment = f"{face}_shields"
        action_id = _ENGINEERING_REVIEW_ACTIONS.get(adjustment) if type(adjustment) is str else None
        if action_id is None:
            return "Ask for one supported adjustment and name the system or shield face."
        tools = getattr(self.wingman, "tools", None)
        has = getattr(tools, "has", None)
        source = getattr(tools, "source", None)
        invoke = getattr(tools, "invoke", None)
        if (callable(has) and callable(source) and callable(invoke)
                and has(_SPOKEN_ENGINEERING_REVIEW)
                and source(_SPOKEN_ENGINEERING_REVIEW) == "StarCitizenComputer"):
            result = await invoke(_SPOKEN_ENGINEERING_REVIEW, {"adjustment": adjustment})
            if result.skill == "StarCitizenComputer" and result.response:
                return result.response
            return "I couldn't verify the engineering review. Check the ship before trying again."
        link = _owner_review_link(
            self.retrieve_custom_property_value("engineering_owner_page", []), action_id,
        )
        if link is None:
            return "Set the local Engineering owner page in Wailing Wilma settings first."
        show_dialog = getattr(getattr(self.wingman, "ui", None), "show_dialog", None)
        if not callable(show_dialog):
            return "This Wingman version cannot open the engineering review dialog."
        await show_dialog(
            "Review one engineering adjustment",
            f"[Select the requested adjustment]({link}) in your open owner panel. "
            "Review the current reading there, then choose Apply if you still want the change.",
        )
        return "The engineering review link is ready. Nothing has been applied."

    def _reset_policy(self):
        with self._state_lock:
            self._policy.reset()
            self._waterfall_route = None

    def _lose_reading(self):
        with self._state_lock:
            self._policy.lose_reading()

    def _telemetry_seat(self):
        """Read only the stable seat identity from Core's freshness-checked snapshot."""
        try:
            telemetry = self.wingman.sc_telemetry
            if getattr(telemetry, "released", True) is False or telemetry.available is not True:
                return None
            state = telemetry.state()
        except Exception:
            return None  # Optional on older hosts or when the stream stops.
        player = state.get("player") if isinstance(state, dict) else None
        if not isinstance(player, dict):
            return None
        mode = player.get("locomotion")
        if mode in ("on_foot", "eva"):
            return ("off_seat",)
        if mode != "seated":
            return None
        vehicle = state.get("vehicle")
        name = vehicle.get("name") if isinstance(vehicle, dict) else None
        seat = player.get("seat")
        return ("seated", seat if isinstance(seat, str) else None,
                name if isinstance(name, str) else None)

    def _context(self):
        if self._screen_source() == "video_playback":
            with self._state_lock:
                self._ship_exit_veto = None
            return _CaptureContext("video_playback")
        with self._state_lock:
            source = getattr(self.wingman, "sc_gamelog", None)
            stable, aboard = self._ship_log_context(source)
            seat = self._telemetry_seat()
            identity = self._ship_log_identity(stable)
            if identity != getattr(self, "_ship_event_identity", None):
                self._ship_exit_veto = None
                self._ship_event_identity = identity
            veto = getattr(self, "_ship_exit_veto", None)
            log_off_seat = False
            if veto is not None:
                try:
                    raw = json.dumps(stable, sort_keys=True, default=str).encode()
                    log_fingerprint = hashlib.sha256(raw).hexdigest()
                    age = time.time() * 1000 - veto["event_ms"]
                    now_ns = time.monotonic_ns()
                    elapsed_ns = now_ns - veto["received_ns"]
                    valid = (seat is None and self._ship_events_active(source)
                             and source.available is True
                             and log_fingerprint == veto["context"]
                             and isinstance(aboard, dict) and aboard.get("value") is False
                             and aboard.get("source_occurrence") == veto["occurrence"]
                             and 0 <= age < self._policy.freshness_ms
                             and 0 <= elapsed_ns
                             and elapsed_ns / 1_000_000 + veto["source_age_ms"]
                             < self._policy.freshness_ms
                             and now_ns < veto["deadline_ns"])
                except Exception:
                    valid = False
                if valid:
                    log_off_seat = True
                else:
                    self._ship_exit_veto = None
        if seat is not None:
            stable = dict(stable or {}, telemetry_seat=seat)
        raw = json.dumps(stable, sort_keys=True, default=str).encode()
        return _CaptureContext(hashlib.sha256(raw).hexdigest(),
                               seat == ("off_seat",) or log_off_seat, bool(seat and seat[0] == "seated"))

    @staticmethod
    def _ship_log_context(source):
        try:
            state = source.state()
            # Core also includes observations/timestamps and changing missions.
            # Those are not a new ship/session and must not reset every fight sample.
            # The public game-log state has no seat identity; do not invent one.
            keys = ("environment", "generation", "session_key", "player_name", "player_geid",
                    "shard", "ship", "ship_class", "ship_owner", "aboard_ship", "own_ship")
            stable = {key: state.get(key) for key in keys} if isinstance(state, dict) else None
            observations = state.get("observations") if stable is not None else None
            aboard = observations.get("aboard_ship") if isinstance(observations, dict) else None
            if (isinstance(aboard, dict) and aboard.get("certainty") == "last_observed"
                    and aboard.get("value") is stable["aboard_ship"]
                    and isinstance(aboard.get("source_occurrence"), str)):
                # A leave and rejoin can end with the same scalar ship/session state.
                stable["aboard_ship_occurrence"] = aboard["source_occurrence"]
            return stable, aboard
        except Exception:
            return None, None

    @staticmethod
    def _ship_log_identity(stable):
        if (not isinstance(stable, dict)
                or any(not isinstance(stable.get(key), str) or not stable[key]
                       for key in ("environment", "generation", "session_key"))
                or not any(isinstance(stable.get(key), str) and stable[key]
                           for key in ("player_name", "player_geid"))):
            return None
        return tuple(stable.get(key) for key in (
            "environment", "generation", "session_key", "player_name", "player_geid", "shard"))

    def _ship_events_active(self, source):
        thread = self._thread
        return (getattr(self, "_ship_event_token", None) is not None
                and source is getattr(self, "_ship_event_source", None)
                and self._generation == getattr(self, "_ship_event_generation", None)
                and not self._stopping and not self._unloading
                and thread is not None and thread.is_alive())

    @staticmethod
    def _ship_event_time_ms(value):
        from datetime import datetime
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return None
        return value.timestamp() * 1000

    def _on_ship_context_event(self, event, token):
        from datetime import datetime
        # Core's public GameEvent has time/environment/status/data, but no source
        # occurrence. Bind it to the current observation's matching timestamp.
        with self._state_lock:
            if token is not getattr(self, "_ship_event_token", None):
                return
            self._ship_exit_veto = None
            source = getattr(self.wingman, "sc_gamelog", None)
            if (not self._ship_events_active(source)
                    or self._screen_source() == "video_playback"
                    or self._telemetry_seat() is not None
                    or getattr(event, "catching_up", None) is not False):
                return
            # Boarding releases immediately; a scalar snapshot never creates a veto.
            if getattr(event, "type", None) != "ship_exited":
                return
            try:
                stable, aboard = self._ship_log_context(source)
                identity = self._ship_log_identity(stable)
                event_ms = self._ship_event_time_ms(getattr(event, "time", None))
                age = time.time() * 1000 - event_ms if event_ms is not None else -1
                data = getattr(event, "data", {})
                if (identity is None or identity != self._ship_event_identity
                        or source.available is not True
                        or getattr(event, "environment", None) != stable["environment"]
                        or getattr(event, "status", None) != "observed"
                        or data.get("action") != "left"
                        or not isinstance(data.get("ship_name"), str) or not data["ship_name"]
                        or stable.get("aboard_ship") is not False
                        or not isinstance(aboard, dict) or aboard.get("value") is not False
                        or aboard.get("certainty") != "last_observed"
                        or not isinstance(aboard.get("source_occurrence"), str)
                        or not aboard["source_occurrence"]
                        or self._ship_event_time_ms(datetime.fromisoformat(
                            aboard.get("timestamp", "").replace("Z", "+00:00"))) != event_ms
                        or event_ms < self._ship_event_started_ms
                        or not 0 <= age < self._policy.freshness_ms):
                    return
                raw = json.dumps(stable, sort_keys=True, default=str).encode()
                received_ns = time.monotonic_ns()
                self._ship_exit_veto = {
                    "context": hashlib.sha256(raw).hexdigest(),
                    "occurrence": aboard["source_occurrence"], "event_ms": event_ms,
                    "received_ns": received_ns, "source_age_ms": age,
                    "deadline_ns": received_ns
                    + int((self._policy.freshness_ms - age) * 1_000_000),
                }
            except Exception:
                return  # Missing/unknown public evidence cannot suppress a reading.

    def _follow_stock_events(self):
        self._unfollow_stock_events()
        source = getattr(self.wingman, "sc_gamelog", None)
        on = getattr(source, "on", None)
        if not callable(on):
            return  # Older hosts: no deduplication, never fewer warnings.
        subscriptions = []
        try:
            subscriptions.append(on("fuel_low", self._on_stock_event))
            self._stock_subscription = tuple(subscriptions)
        except Exception:
            pass
        ship_subscriptions = []
        try:
            self._ship_event_token = token = object()
            self._ship_event_source = source
            self._ship_event_generation = self._generation
            self._ship_event_started_ms = int(time.time() * 1000)
            stable, _aboard = self._ship_log_context(source)
            self._ship_event_identity = self._ship_log_identity(stable)
            for kind, callback in (
                ("ship_exited", lambda event: self._on_ship_context_event(event, token)),
                ("ship_entered", lambda event: self._on_ship_context_event(event, token)),
            ):
                ship_subscriptions.append(on(kind, callback))
                self._stock_subscription = tuple(subscriptions + ship_subscriptions)
        except Exception:
            # Older suppliers may support fuel events but not both ship events.
            # A partial veto subscription is unusable; preserve independent fuel.
            self._ship_event_token = self._ship_event_source = self._ship_exit_veto = None
            self._stock_subscription = tuple(subscriptions)
            for item in ship_subscriptions:
                try:
                    item.unsubscribe()
                except Exception:
                    pass

    def _unfollow_stock_events(self):
        subscription, self._stock_subscription = self._stock_subscription, None
        self._ship_event_token = self._ship_event_source = self._ship_exit_veto = None
        for item in subscription or ():
            try:
                item.unsubscribe()
            except Exception:
                pass

    def _on_stock_event(self, event):
        # A raw game-log event is not evidence that another skill spoke. Keep
        # Wilma's warning available unless the host exposes an announcement receipt.
        del event

    def _same(self, generation, scope, context):
        window, _ = self._window()
        return (generation == self._generation and window is not None
                and window.scope == scope and self._context() == context
                and self._read_reader() == self._reader_signature)

    def _screen_source(self):
        value = self.retrieve_custom_property_value("screen_source", [])
        return "game" if value is None else value

    def _window(self):
        return foreground_game(source=self._screen_source())

    def _read_reader(self):
        return read_reader_settings(lambda name: self.retrieve_custom_property_value(name, []))

    def _busy(self):
        return (self._muted or self.wingman.conversation.user_busy
                or self.wingman.audio.is_playing_at_least(SpeechPriority.EVENT))

    async def _owned(self, awaitable, timeout, *, bounded=False):
        # Most runtime calls keep the original contract: settle the owned worker
        # before returning or raising so callers can safely release its inputs.
        # Only first-use setup probes may opt into a bounded unknown result.
        task = asyncio.ensure_future(awaitable)
        if bounded:
            self._owned_tasks.add(task)

            def settled(done):
                self._owned_tasks.discard(done)
                if not done.cancelled():
                    try:
                        done.exception()
                    except Exception:
                        pass

            task.add_done_callback(settled)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            if bounded:
                return _OWNED_TIMEOUT  # Unknown status; the child remains owned.
            raise
        finally:
            if not bounded and not task.done():
                await asyncio.gather(task, return_exceptions=True)

    def _encode_capture(self, image):
        detail = "stock" if self._using_stock_detail else self._image_detail
        if detail == "stock":
            return encode_capture(image, detail, vision_base64)
        # Older v3 hosts may not provide the optional priority helper.
        try:
            from services.thread_priority import below_normal_priority
        except ModuleNotFoundError as error:
            if error.name != "services.thread_priority":
                raise
            return encode_capture(image, detail, vision_base64)

        with below_normal_priority():
            return encode_capture(image, detail, vision_base64)

    async def _sample(self, generation):
        window, reason = self._window()
        if window is None:
            self._lose_reading()
            self._status = reason
            return
        reader_signature = self._read_reader()
        if reader_signature != self._reader_signature:
            self._reset_policy()
            self._reader_signature = reader_signature
            self._using_stock_detail = False
        reader, secret_name = reader_signature
        context = self._context()
        scope = window.scope + ":" + str(context.fingerprint)
        if scope != self._scope:
            self._reset_policy()
            self._scope = scope
        terminals_on_foot = self.retrieve_custom_property_value("watch_terminals_on_foot", []) is not False
        personal_categories = sorted(self._policy.personal_status_categories)
        if "suit_oxygen" in self._policy.category_thresholds:
            personal_categories.insert(0, "suit_oxygen")
        prompt = _PROMPT
        if context.off_seat and not terminals_on_foot:
            configured_personal = bool(personal_categories)
            silent = self._policy.muted_categories | self._policy.voice_muted
            personal_categories = [category for category in personal_categories if category not in silent]
            if not personal_categories:
                self._lose_reading()
                self._status = ("personal warnings muted; on-foot capture paused"
                                if configured_personal else "waiting for you to take a seat")
                return
            prompt = build_prompt(personal_categories)
        # Older public v3 facades have generate(image=...) but no capability
        # metadata helper. Missing metadata is unknown, not a broken provider.
        sees_images = (getattr(self.wingman.ai, "sees_images", None)
                       if reader.provider == "wingman" else None)
        if callable(sees_images) and await self._owned(sees_images(), 10) is False:
            self._reset_policy()
            self._status = "configured model does not support images"
            return
        api_key = None
        if secret_name:
            api_key = await self._owned(self.wingman.secrets.retrieve(secret_name), 10)
            if not api_key:
                self._reset_policy()
                self._status = "selected vision reader has no stored credential"
                return
        if not self._same(generation, window.scope, context):
            self._lose_reading()
            return
        captured = time.monotonic_ns() // 1_000_000
        captured_at = utc_timestamp()
        capture = asyncio.ensure_future(self.wingman.screen.capture(
            display=1, region=window.region, dxcam=False))
        try:
            image = await self._owned(capture, 10)
        except BaseException:
            # Cancellation can arrive before the assignment acquires the image.
            # _owned settled the capture; close its result even on that path.
            if capture.done() and not capture.cancelled() and capture.exception() is None:
                close = getattr(capture.result(), "close", None)
                if callable(close):
                    close()
            raise
        try:
            if not self._same(generation, window.scope, context):
                self._lose_reading()
                return
            encoded = await self._owned(asyncio.to_thread(self._encode_capture, image), 10)
        finally:
            # Stock capture returns a fresh PIL image. The owned worker settles
            # before releasing its pixels, including on cancellation.
            close = getattr(image, "close", None)
            if callable(close):
                close()
        if not self._same(generation, window.scope, context):
            self._lose_reading()
            return
        remaining_ms = self._policy.freshness_ms - (time.monotonic_ns() // 1_000_000 - captured)
        if remaining_ms <= 0:
            self._lose_reading()
            self._status = "capture too old before vision request"
            return
        try:
            image_url = "data:image/jpeg;base64," + encoded
            diagnostics = {} if reader.provider == "waterfall" else None
            if reader.provider == "wingman":
                request = asyncio.wait_for(
                    self.wingman.ai.generate(
                        prompt,
                        system="Report visible text only; do not infer missing information.",
                        image=image_url,
                    ),
                    remaining_ms / 1000,
                )
            else:
                request = read_image(
                    reader, prompt, image_url, api_key=api_key,
                    timeout_seconds=remaining_ms / 1000,
                    diagnostics=diagnostics,
                    deadline_monotonic_seconds=(captured + self._policy.freshness_ms) / 1000,
                )
            text = await self._owned(request, 30)
        except asyncio.CancelledError:
            # A provider may cancel its individual request while the visual worker is healthy.
            if generation != self._generation or asyncio.current_task().cancelling():
                raise
            self._lose_reading()
            self._status = "vision reader cancelled this frame"
            await self._reader_reachable(False, generation)
            return
        except TimeoutError:
            self._lose_reading()
            self._status = "vision response too old; use a faster model or adjust reading age"
            await self._reader_reachable(False, generation)
            return
        except ReaderUnavailable as error:
            self._lose_reading()
            if reader.provider == "ollama" and error.context_exceeded:
                if not self._same(generation, window.scope, context):
                    return
                if self._image_detail == "high" and not self._using_stock_detail:
                    self._using_stock_detail = True
                    self._status = "watching; next capture will use stock image detail"
                else:
                    self._status = "the image request exceeds Ollama's context even at stock detail"
                return
            self._status = "selected vision reader unavailable: " + str(error)
            await self._reader_reachable(False, generation)
            return
        except Exception as error:
            self._lose_reading()
            # Avoid leaking URLs, provider bodies or secrets through status.
            self._status = "vision request failed: " + type(error).__name__
            await self._reader_reachable(False, generation)
            return
        if not self._same(generation, window.scope, context):
            self._lose_reading()
            return
        now = time.monotonic_ns() // 1_000_000
        if now - captured >= self._policy.freshness_ms:
            with self._state_lock:
                self._policy.observe((), capture_id=str(uuid4()), captured_ms=captured,
                                     now_ms=now, scope=scope, scene="unknown")
            self._status = "vision response too old; use a faster model or adjust reading age"
            return
        try:
            report = parse_visual_report(text)
        except ValueError:
            self._lose_reading()
            self._status = "vision response has no usable structured readings"
            return
        capture_id = str(uuid4())
        scene = report.scene
        if context.seated and scene == "fps":
            scene = "unknown"
        if context.off_seat and scene == "engineering_terminal" and not terminals_on_foot:
            scene = "unknown"
        if context.off_seat and scene in ("cockpit", "ship_exterior", "ground_vehicle"):
            scene = "fps"  # A visible cockpit need not be the player's own camera.
        with self._state_lock:
            notices = self._policy.observe(report.readings, capture_id=capture_id,
                captured_ms=captured, now_ms=now, scope=scope, scene=scene,
                terminal=report.terminal)
            if diagnostics and (report.readings or not report.rejected_count):
                self._waterfall_route = diagnostics.get("waterfall_route")
        self._status = "watching"
        if scene == "fps" and personal_categories:
            self._status = "watching your " + _spoken(personal_categories) + " on foot"
        elif scene == "ground_vehicle":
            self._status = "watching your ground vehicle"
        elif scene not in VEHICLE_SCENES:
            self._status = "watching; " + _SCENE_PLAIN[scene] + ", so no ship warnings"
        elif report.scene == "engineering_terminal" and report.terminal is None:
            self._status = "watching; terminal page or subject unclear, so no ship warnings"
        elif report.rejected_count and not report.readings:
            self._status = "vision response has no usable readings"
        if report.rejected_count:
            self._status += f"; {report.rejected_count} reading(s) rejected"
            reason = max(report.rejection_reasons, key=report.rejection_reasons.get)
            self._status += " (" + reason.replace("_", " ") + ")"
        # A reply only restores service once the current frame can be evaluated.
        reader_update = (self._reader_transition(True, generation)
                         if report.readings or not report.rejected_count else None)
        if not notices:
            if reader_update:
                await self._speak_reader_update(reader_update)
            return
        evidence = FeedbackCapture(capture_id, captured_at, self._screen_source(), scene, context.fingerprint)
        await self._owned(self._speak_warning(
            notices, generation, window.scope, context, reader_update,
            evidence=evidence, encoded=encoded, reader=reader, captured=captured,
        ), 15)

    async def _speak_warning(self, notices, generation, window_scope, context, reader_update,
                             *, evidence, encoded, reader, captured):
        # Check in the task that calls the host, after it has actually started.
        if not self._same(generation, window_scope, context):
            self._lose_reading()
            return
        if self._busy():
            return
        # Fold this frame's warnings into one utterance (critical first, bounded)
        # so a fight burst neither plays stale nor builds a speech queue.
        with self._state_lock:
            if generation != self._generation or self._muted:
                return
            submitted = time.monotonic_ns() // 1_000_000
            burst = self._policy.current_burst(notices, now_ms=submitted)
        if not burst:
            return
        line = fold_text(burst)
        if reader_update:
            line = f"{line}; {reader_update}"
        try:
            # Core BACKGROUND is the public non-queueing speech mode. EVENT
            # can wait behind a reply after our precheck and play stale text.
            spoken = await self.wingman.tts.speak(line, priority=SpeechPriority.BACKGROUND)
        except BaseException:
            with self._state_lock:
                if generation == self._generation:
                    self._last_warning_submission = None
                self._policy.clear_warning_reference()
                self._policy.attempted_burst(burst)  # unknown delivery: don't repeat blindly
            raise
        if spoken is not False:
            snapshot = WarningSubmission(
                str(uuid4()), evidence, hashlib.sha256(encoded.encode()).hexdigest(),
                submitted - captured, reader.provider, reader.model,
                tuple(FeedbackNotice(notice.category, notice.severity, notice.text) for notice in burst),
            ) if spoken is True else None
            with self._state_lock:
                if generation == self._generation:
                    self._last_warning_submission = snapshot
                    self._policy.record_warning_delivery(
                        burst, spoken=spoken is True and not self._muted,
                        now_ms=time.monotonic_ns() // 1_000_000,
                    )
                self._policy.attempted_burst(burst)

    async def _reader_reachable(self, reachable, generation=None):
        line = self._reader_transition(reachable, generation)
        if line:
            await self._speak_reader_update(line)

    def _reader_transition(self, reachable, generation):
        """Record reader availability and return any announcement it warrants."""
        if generation is not None and generation != self._generation:
            return None
        if self._reader_down is (not reachable):
            return None
        was_unknown = self._reader_down is None
        self._reader_down = not reachable
        if (reachable and was_unknown) or self._muted:
            return None
        return READER_BACK_LINE if reachable else READER_DOWN_LINE

    async def _speak_reader_update(self, line):
        try:
            await self._owned(
                self.wingman.tts.speak(line, priority=SpeechPriority.BACKGROUND), 15)
        except Exception:
            pass  # The status line keeps the exact reason; speech is best effort.

    async def _run(self, generation):
        while generation == self._generation:
            started = time.monotonic()
            try:
                await self._sample(generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._lose_reading()
                self._status = "visual source unavailable"
            if generation != self._generation:
                return
            if self._status.startswith("watching"):
                # Set the minimum time between the starts of checks. A slow
                # result already spent that interval; request fresh pixels next.
                delay = max(0.0, self._poll_interval - (time.monotonic() - started))
            else:
                # Missing sources and reader failures still receive a full backoff.
                delay = max(1.0, self._poll_interval)
            await asyncio.sleep(delay)
