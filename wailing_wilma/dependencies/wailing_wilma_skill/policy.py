"""Passive deterministic alerts over unqualified visual estimates."""
from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Severity = Literal["warning", "critical"]
_PERCENT = frozenset({
    "shield_charge", "component_health", "fuel_percent",
    "quantum_fuel_percent", "oxygen_percent", "hull_integrity_percent",
    "armor_percent",
})
_BOOLEAN = frozenset({
    "power_online", "overheating", "component_failed",
    "environment_warning", "emp_warning", "fire_warning",
})
_KINDS = _PERCENT | _BOOLEAN
_PERCENT_CATEGORIES = frozenset({
    "shields", "components", "fuel", "quantum_fuel", "life_support", "hull", "armor",
})
_CATEGORIES = _PERCENT_CATEGORIES | frozenset({"power", "heat", "environment", "emp", "fire"})
_MAX_TEXT = 96_000
_MAX_ROWS = 128
_MAX_TRACKS = 128
_MAX_MS = 2**63 - 1
_WS = re.compile(r"\s+")
_LABEL = re.compile(r"[^A-Za-z0-9 _-]+", re.ASCII)
_EMP = re.compile(r"(?<![A-Za-z0-9])EMP(?![A-Za-z0-9])", re.IGNORECASE)
_PERCENT_TEXT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?%", re.ASCII)
_PERCENT_MENTION = re.compile(
    r"(?<![A-Za-z0-9.+-])(?P<number>[+-]?(?:[0-9][0-9,/_]*(?:\.[0-9]*)?|\.[0-9]+)"
    r"(?:[eE][+-]?[0-9]+)?)%", re.ASCII,
)
_HUD_RESOURCE_PERCENT = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:AB|H)\s*[0-9]+(?:\.[0-9]+)?%"
    r"|[0-9]+(?:\.[0-9]+)?%\s*(?:AB|H)(?![A-Za-z0-9]))", re.I | re.ASCII,
)
_QUANTUM_FUEL = re.compile(
    r"(?<![A-Za-z0-9])(?:Q[-\s]*FUEL|QUANTUM\s+FUEL)(?![A-Za-z0-9])",
    re.I | re.ASCII,
)
_HYDROGEN_FUEL = re.compile(
    r"(?<![A-Za-z0-9])(?:H[-\s]*FUEL|HYDROGEN(?:\s+FUEL)?)(?![A-Za-z0-9])",
    re.I | re.ASCII,
)
_FUEL_GAUGES = {"fuel_percent": "hydrogen fuel", "quantum_fuel_percent": "quantum fuel"}
_EMPTY_FUEL_GAUGE = re.compile(r"(hydrogen fuel|quantum fuel)(?:\s*:\s*|\s+)empty", re.I)
_COMPONENT_STATE_KINDS = frozenset({
    "power_online", "component_failed", "component_health", "overheating",
})
_PAGE_HEADINGS = frozenset({"output", "power management", "power management output"})
_SHIELD_SUFFIX = re.compile(r"\s+shields?$", re.IGNORECASE | re.ASCII)
_SHIELD_DIRECTIONS = {
    "front": "front", "forward": "front", "fore": "front",
    "rear": "rear", "aft": "rear", "back": "rear",
    "left": "port", "port": "port", "right": "starboard", "starboard": "starboard",
    "top": "top", "upper": "top", "dorsal": "top",
    "bottom": "bottom", "lower": "bottom", "ventral": "bottom",
}
_SHIELD_DIRECTION = re.compile(
    r"\b(?:" + "|".join(_SHIELD_DIRECTIONS) + r")\b", re.IGNORECASE | re.ASCII,
)
_FAILURE_STATUS = re.compile(
    r"\b(?:failed|failure|destroyed|inoperable|broken)\b", re.IGNORECASE | re.ASCII,
)
_NEGATED_FAILURE = re.compile(
    r"\b(?:not|no|never|without|isn't|isnt|wasn't|wasnt)\b(?:\W+\w+){0,2}"
    r"\W+\b(?:failed|failure|destroyed|inoperable|broken)\b|"
    r"\b(?:failed|failure|destroyed|inoperable|broken)\b(?:\W+\w+){0,2}"
    r"\W+\b(?:not|absent|cleared|resolved)\b",
    re.IGNORECASE | re.ASCII,
)
_POWER_STATES = {True: ("online", "on", "enabled"), False: ("offline", "off", "disabled")}
_JSON_FENCE = re.compile(r"\A```json[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _constant(value: str) -> object:
    raise ValueError(f"invalid JSON number {value}")


def _text(value: object, name: str, limit: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"invalid {name}")
    return value


def _number(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("number must be finite") from exc
    if not math.isfinite(result):
        raise ValueError("number must be finite")
    return result


@dataclass(frozen=True, slots=True)
class Reading:
    kind: str
    label: str
    value: float | bool
    evidence: str
    source: str = "vision_model"


@dataclass(frozen=True, slots=True)
class Notice:
    key: str
    category: str
    severity: Severity
    text: str
    expires_ms: int


SCENES = ("cockpit", "ship_exterior", "engineering_terminal", "fps", "menu_or_map",
          "cutscene_or_loading", "unknown")
# A terminal is eligible only for identifiable current ship-system readings.
SHIP_SCENES = frozenset({"cockpit", "ship_exterior", "engineering_terminal"})


@dataclass(frozen=True, slots=True)
class TerminalView:
    page: str
    subject: str

    def __post_init__(self) -> None:
        for key in ("page", "subject"):
            token = _WS.sub(" ", _text(getattr(self, key), key, 80)).casefold()
            if token in {"unknown", "none", "unreadable", "n/a"} or not any(
                    character.isalnum() for character in token):
                raise ValueError("terminal identity must be visible text")
            object.__setattr__(self, key, token)


def _terminal_view(payload: object) -> TerminalView | None:
    if type(payload) is not dict or set(payload) != {"page", "subject"}:
        return None
    try:
        return TerminalView(page=payload["page"], subject=payload["subject"])
    except ValueError:
        return None


def _visual_envelope(text: str) -> tuple[list[object], str, TerminalView | None]:
    """Decode the bounded envelope and optional literal terminal identity."""
    if type(text) is not str or len(text) > _MAX_TEXT:
        raise ValueError("visual response must be bounded text")
    fenced = _JSON_FENCE.fullmatch(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        payload = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid visual JSON") from exc
    if (type(payload) is not dict or "readings" not in payload
            or not set(payload) <= {"readings", "scene", "terminal"}):
        raise ValueError("visual JSON has unknown keys or missing readings")
    rows = payload["readings"]
    if type(rows) is not list or len(rows) > _MAX_ROWS:
        raise ValueError("readings must be a bounded list")
    scene = payload.get("scene")
    scene = scene.strip().casefold() if type(scene) is str else "unknown"
    terminal = _terminal_view(payload.get("terminal")) if scene == "engineering_terminal" else None
    return rows, scene if scene in SCENES else "unknown", terminal


def _visual_rows(text: str) -> list[object]:
    return _visual_envelope(text)[0]


def _parse_row(row: object) -> Reading:
    if type(row) is not dict or set(row) != {"kind", "label", "value", "evidence"}:
        raise ValueError("reading has missing or unknown keys")
    kind = row["kind"]
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("unsupported reading kind")
    label = _text(row["label"], "label", 80)
    evidence = _text(row["evidence"], "evidence", 512)
    value = row["value"]
    if kind in _PERCENT:
        if type(value) is str and _PERCENT_TEXT.fullmatch(value):
            value = float(value[:-1])
        if type(value) not in (int, float):
            raise ValueError("percentage must be numeric")
        value = _number(value)
        if not 0 <= value <= 100:
            raise ValueError("percentage must be between 0 and 100")
        gauge = _FUEL_GAUGES.get(kind)
        empty = _EMPTY_FUEL_GAUGE.fullmatch(_WS.sub(" ", evidence)) if gauge else None
        if empty is not None:
            label_name = _WS.sub(" ", label).casefold()
            if value != 0 or empty[1].casefold() != gauge or label_name != gauge:
                raise ValueError("empty fuel evidence must match the named gauge at zero")
        else:
            mentions = _PERCENT_MENTION.findall(evidence)
            if not mentions or any(
                not _PERCENT_TEXT.fullmatch(number + "%") for number in mentions
            ):
                raise ValueError("percentage evidence must contain a literal decimal percentage")
            numeric_mentions = {Decimal(number) for number in mentions}
            if len(numeric_mentions) != 1 or Decimal(str(value)) not in numeric_mentions:
                raise ValueError("percentage value must match one unambiguous literal percentage")
        if (kind in {"shield_charge", "hull_integrity_percent", "armor_percent"}
                and _HUD_RESOURCE_PERCENT.search(evidence)):
            raise ValueError("HUD resource percentage does not establish this reading")
        if kind == "shield_charge":
            label_faces = {_SHIELD_DIRECTIONS[word.casefold()]
                           for word in _SHIELD_DIRECTION.findall(label)}
            if label_faces:
                evidence_faces = {_SHIELD_DIRECTIONS[word.casefold()]
                                  for word in _SHIELD_DIRECTION.findall(evidence)}
                if len(label_faces) != 1 or label_faces != evidence_faces:
                    raise ValueError("directional shield evidence must match one visible face")
        if (kind == "fuel_percent" and _QUANTUM_FUEL.search(label + " " + evidence)
                or kind == "quantum_fuel_percent"
                and _HYDROGEN_FUEL.search(label + " " + evidence)):
            raise ValueError("explicit fuel identity contradicts the reading category")
    elif type(value) is not bool:
        raise ValueError("boolean reading must be true or false")
    if (kind in _COMPONENT_STATE_KINDS
            and _WS.sub(" ", label).casefold() in _PAGE_HEADINGS):
        raise ValueError("page heading is not a component identity")
    if kind == "component_failed" and value is True:
        if not _FAILURE_STATUS.search(evidence) or _NEGATED_FAILURE.search(evidence):
            raise ValueError("component failure requires explicit positive failure evidence")
    if kind == "power_online":
        name = re.escape(_WS.sub(" ", label).casefold())
        states = "|".join(_POWER_STATES[value])
        pair = rf"(?:{name}(?: *: *| +)(?:{states})|(?:{states}) +{name})[.]?"
        if not re.fullmatch(pair, _WS.sub(" ", evidence).casefold()):
            raise ValueError("power evidence must pair the component with its explicit state")
    return Reading(kind, label, value, evidence)


def _identity(row: object) -> tuple[str, str] | None:
    if type(row) is not dict:
        return None
    kind, label = row.get("kind"), row.get("label")
    if type(kind) is not str or kind not in _KINDS:
        return None
    try:
        label = _text(label, "label", 80)
    except ValueError:
        return None
    return kind, _WS.sub(" ", label).casefold()


@dataclass(frozen=True, slots=True)
class VisualParseReport:
    readings: tuple[Reading, ...]
    rejected_count: int
    scene: str = "unknown"
    terminal: TerminalView | None = None


def parse_visual_report(text: str) -> VisualParseReport:
    """Keep valid independent readings; reject every participant in a conflict."""
    rows, scene, terminal = _visual_envelope(text)
    groups: dict[tuple[str, str], list[Reading | None]] = {}
    unkeyed = 0
    for row in rows:
        identity = _identity(row)
        try:
            reading = _parse_row(row)
        except ValueError:
            reading = None
        if identity is None:
            unkeyed += 1
        else:
            groups.setdefault(identity, []).append(reading)
    accepted = []
    rejected = unkeyed
    for readings in groups.values():
        first = readings[0]
        if first is None or any(
            item is None or item.value != first.value
            or item.evidence.casefold() != first.evidence.casefold()
            for item in readings
        ):
            rejected += len(readings)
        else:
            accepted.append(first)
    return VisualParseReport(tuple(accepted), rejected, scene, terminal)


def parse_visual(text: str) -> tuple[Reading, ...]:
    """Strict default: reject any invalid or conflicting reading."""
    report = parse_visual_report(text)
    if report.rejected_count:
        raise ValueError("invalid or conflicting visual reading")
    return report.readings


@dataclass(slots=True)
class _Track:
    active: int = 0
    candidate: tuple[str, int] | None = None
    captures: set[str] = field(default_factory=set)
    known_power: bool | None = None
    last_attempt_ms: int | None = None
    last_attempt_severity: Severity | None = None
    seen_ms: int = 0


class WilmaPolicy:
    """Confirm distinct captures and offer threshold/transition notices only."""

    def __init__(
        self,
        *,
        low_percent: float = 20.0,
        critical_percent: float = 5.0,
        recovery_hysteresis: float = 5.0,
        category_thresholds: Mapping[str, tuple[float, float]] | None = None,
        muted_categories: set[str] | frozenset[str] | None = None,
        confirmations: int = 2,
        repeat_cooldown_ms: int = 60_000,
        freshness_ms: int = 3_000,
        burst_limit: int = 3,
    ) -> None:
        values = (_number(low_percent), _number(critical_percent), _number(recovery_hysteresis))
        low, critical, hysteresis = values
        if not 0 <= critical <= low <= 100 or not 0 <= hysteresis <= 100:
            raise ValueError("percent thresholds and hysteresis must be within 0..100")
        thresholds: dict[str, tuple[float, float]] = {}
        if category_thresholds is not None:
            if not isinstance(category_thresholds, Mapping):
                raise ValueError("category_thresholds must be a mapping")
            for category, pair in category_thresholds.items():
                if type(category) is not str or category not in _PERCENT_CATEGORIES:
                    raise ValueError("category threshold must name a percentage category")
                if type(pair) is not tuple or len(pair) != 2:
                    raise ValueError("category threshold must be a low/critical pair")
                if any(type(value) not in (int, float) for value in pair):
                    raise ValueError("category thresholds must be numeric")
                category_low, category_critical = (_number(value) for value in pair)
                if not 0 <= category_critical <= category_low <= 100:
                    raise ValueError("category thresholds must satisfy 0 <= critical <= low <= 100")
                thresholds[category] = category_low, category_critical
        if muted_categories is None:
            muted: frozenset[str] = frozenset()
        else:
            if type(muted_categories) not in (set, frozenset):
                raise ValueError("muted_categories must be a set of categories")
            if any(type(category) is not str or category not in _CATEGORIES
                   for category in muted_categories):
                raise ValueError("muted_categories contains an unknown category")
            muted = frozenset(muted_categories)
        if type(confirmations) is not int or not 1 <= confirmations <= 64:
            raise ValueError("confirmations must be between 1 and 64")
        if type(burst_limit) is not int or not 1 <= burst_limit <= 8:
            raise ValueError("burst_limit must be between 1 and 8")
        for name, value, minimum in (
            ("repeat_cooldown_ms", repeat_cooldown_ms, 0),
            ("freshness_ms", freshness_ms, 1),
        ):
            if type(value) is not int or not minimum <= value <= _MAX_MS:
                raise ValueError(f"{name} must be a bounded nonnegative millisecond value")
        self.low_percent, self.critical_percent = low, critical
        self.recovery_hysteresis = hysteresis
        self.category_thresholds = thresholds
        self.muted_categories = muted
        # Voice mutes last until unmuted or the skill reloads; they are not saved.
        self.voice_muted: frozenset[str] = frozenset()
        # Voice-set low levels last until reset or the skill reloads; not saved.
        self.voice_low: dict[str, float] = {}
        self.confirmations = confirmations
        self.repeat_cooldown_ms, self.freshness_ms = repeat_cooldown_ms, freshness_ms
        self.burst_limit = burst_limit
        self._scope: object | None = None
        self._ship_view: tuple[str, TerminalView | None] | None = None
        self._tracks: dict[tuple[str | None, str], _Track] = {}
        self._offers: dict[str, Notice] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._last_capture_ms: int | None = None
        self._last_now_ms: int | None = None
        self._checked: tuple[Notice, ...] = ()
        self._external: dict[str, int] = {}
        self._visible: frozenset[str] = frozenset()

    @staticmethod
    def _clock(value: object, name: str) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_MS:
            raise ValueError(f"{name} must be a nonnegative millisecond timestamp")
        return value

    @staticmethod
    def _key(reading: Reading) -> str:
        return f"{reading.kind}:{_WS.sub(' ', reading.label.strip()).casefold()}"

    @staticmethod
    def _label(value: str) -> str:
        value = _WS.sub(" ", _LABEL.sub("", value)).strip(" _-")[:48]
        return value.title() if value else "System"

    @staticmethod
    def _category(kind: str) -> str:
        return {
            "shield_charge": "shields", "component_health": "components",
            "fuel_percent": "fuel", "quantum_fuel_percent": "quantum_fuel",
            "oxygen_percent": "life_support", "power_online": "power",
            "hull_integrity_percent": "hull", "armor_percent": "armor",
            "overheating": "heat", "component_failed": "components",
            "environment_warning": "environment", "emp_warning": "emp",
            "fire_warning": "fire",
        }[kind]

    def _validate_reading(self, row: Reading) -> None:
        if not isinstance(row, Reading) or row.kind not in _KINDS:
            raise ValueError("unsupported reading")
        _text(row.label, "label", 80)
        _text(row.evidence, "evidence", 512)
        _text(row.source, "source", 80)
        if row.kind in _PERCENT:
            if type(row.value) not in (int, float) or not 0 <= _number(row.value) <= 100:
                raise ValueError("percentage must be finite and between 0 and 100")
        elif type(row.value) is not bool:
            raise ValueError("boolean reading must be bool")

    def levels(self, category: str) -> tuple[float, float]:
        """Effective (low, critical) percentages for a category right now."""
        low, critical = self.category_thresholds.get(
            category, (self.low_percent, self.critical_percent),
        )
        return self.voice_low.get(category, low), critical

    def _severity(self, track: _Track, value: float, category: str) -> int:
        low, critical = self.levels(category)
        if track.active == 2:
            if value <= critical + self.recovery_hysteresis:
                return 2
            return 1 if value <= low else 0
        if track.active == 1:
            if value <= critical:
                return 2
            return 1 if value <= low + self.recovery_hysteresis else 0
        if value <= critical:
            return 2
        return 1 if value <= low else 0

    def _confirmed(self, track: _Track, state: tuple[str, int], capture_id: str) -> bool:
        if track.candidate != state:
            track.candidate, track.captures = state, set()
        track.captures.add(capture_id)
        return len(track.captures) >= self.confirmations

    @staticmethod
    def _severity_name(level: int) -> Severity:
        return "critical" if level == 2 else "warning"

    def _offer(self, row: Reading, key: str, level: int, captured_ms: int) -> Notice:
        label = self._label(row.label)
        if row.kind in _PERCENT:
            value = float(row.value)
            if row.kind == "shield_charge":
                label = _SHIELD_SUFFIX.sub("", label).strip() or "System"
                text = f"{label} shields depleted" if value == 0 else (
                    f"{label} shields {'critically low' if level == 2 else 'low'} ({value:g}%)")
            else:
                text = f"{label} {'critically low' if level == 2 else 'low'} ({value:g}%)"
        else:
            text = {
                "power_online": f"Power offline: {label}",
                "overheating": f"{label} overheating",
                "component_failed": f"{label} failed",
                "environment_warning": f"Environment warning: {label}",
                "emp_warning": f"EMP warning: {label}",
                "fire_warning": f"Fire: {label}",
            }[row.kind]
        notice = Notice(key, self._category(row.kind), self._severity_name(level), text,
                        min(captured_ms + self.freshness_ms, _MAX_MS))
        self._offers[key] = notice
        return notice

    def _clear_pending(self) -> None:
        self._offers.clear()
        self._checked = ()
        self._visible = frozenset()
        for track in self._tracks.values():
            track.candidate, track.captures = None, set()

    def _reset_scope(self, scope: object | None) -> None:
        self._scope, self._tracks = scope, {}
        self._ship_view = None
        self._offers.clear()
        self._seen.clear()
        self._last_capture_ms = None
        self._checked = ()
        self._external.clear()
        self._visible = frozenset()

    def _change_ship_view(self, view: tuple[str, TerminalView | None] | None) -> None:
        self._clear_pending()
        for track in self._tracks.values():
            track.known_power = None
        self._ship_view = view

    def _track_key(self, reading_key: str) -> tuple[str | None, str]:
        terminal = self._ship_view[1] if self._ship_view is not None else None
        return (None if terminal is None else terminal.subject, reading_key)

    def _remember_capture(self, capture_id: str, captured_ms: int) -> None:
        self._last_capture_ms = captured_ms
        self._seen[capture_id] = None
        if len(self._seen) > 64:
            self._seen.popitem(last=False)

    def note_external(self, category: str, *, now_ms: int) -> None:
        """Record that another announcer just spoke a warning for ``category``.

        Wingman's stock Star Citizen Events skill already speaks some game-log
        warnings, such as low fuel. Wilma then withholds its own warning-level
        notice for that category until one repeat cooldown has passed, counting
        the stock announcement as the attempt. A critical escalation is still
        offered because it is new information.
        """
        now_ms = self._clock(now_ms, "now_ms")
        if type(category) is not str or category not in _CATEGORIES:
            raise ValueError("unknown warning category")
        self._external[category] = max(now_ms, self._external.get(category, 0))
        for key, notice in list(self._offers.items()):
            if notice.category == category and notice.severity == "warning":
                del self._offers[key]
                self._checked = tuple(item for item in self._checked if item is not notice)
                self._spent_externally(key, now_ms)

    def _spent_externally(self, key: str, noted_ms: int) -> None:
        track = self._tracks.get(self._track_key(key))
        if track is not None and (track.last_attempt_ms is None
                                  or track.last_attempt_ms < noted_ms):
            track.last_attempt_ms, track.last_attempt_severity = noted_ms, "warning"

    def observe(
        self, readings: tuple[Reading, ...], *, capture_id: str,
        captured_ms: int, now_ms: int, scope: object, scene: str = "cockpit",
        terminal: TerminalView | None = None,
    ) -> tuple[Notice, ...]:
        now_ms, captured_ms = self._clock(now_ms, "now_ms"), self._clock(captured_ms, "captured_ms")
        if captured_ms > now_ms or scope is None:
            raise ValueError("capture must be current and scope caller-owned")
        capture_id = _text(capture_id, "capture_id", 128)
        if type(scope) is str and (not scope.strip() or len(scope) > 512):
            raise ValueError("invalid scope")
        if not isinstance(readings, tuple) or len(readings) > _MAX_ROWS:
            raise ValueError("readings must be a bounded tuple")
        for row in readings:
            self._validate_reading(row)

        if self._scope is not None and scope != self._scope:
            self._reset_scope(scope)
        elif self._scope is None:
            self._scope = scope
        if self._last_now_ms is not None and now_ms < self._last_now_ms:
            self._clear_pending()
            raise ValueError("policy clock moved backwards")
        self._last_now_ms = now_ms
        if now_ms - captured_ms > self.freshness_ms:
            self.lose_reading()
            return ()
        if scene != "engineering_terminal":
            terminal = None
        if scene not in SHIP_SCENES or (scene == "engineering_terminal" and terminal is None):
            # An older or duplicate frame can withdraw offers, but cannot
            # replace a newer scene's power baseline.
            if (capture_id not in self._seen and (self._last_capture_ms is None
                    or captured_ms > self._last_capture_ms)):
                self._change_ship_view(None)
                self._remember_capture(capture_id, captured_ms)
            else:
                self._clear_pending()
            return ()
        if capture_id in self._seen:
            return ()
        if self._last_capture_ms is not None and captured_ms <= self._last_capture_ms:
            self._clear_pending()
            return ()

        view = (scene, terminal)
        if view != self._ship_view:
            self._change_ship_view(view)

        self._offers.clear()
        self._checked = ()
        self._remember_capture(capture_id, captured_ms)

        by_key: dict[str, Reading] = {}
        for row in readings:
            key = self._key(row)
            if key in by_key:
                raise ValueError("duplicate reading")
            by_key[key] = row
        self._visible = frozenset(self._category(row.kind) for row in by_key.values())
        visible_tracks = {self._track_key(key) for key in by_key}
        for track_key, track in self._tracks.items():
            if track_key not in visible_tracks:
                track.candidate, track.captures = None, set()
                # Missing is unknown: a later OFF cannot prove a transition
                # from an ON state that was no longer observed.
                track.known_power = None

        notices = []
        for key, row in by_key.items():
            track_key = self._track_key(key)
            track = self._tracks.get(track_key)
            if track is None:
                if len(self._tracks) >= _MAX_TRACKS:
                    oldest = min(self._tracks, key=lambda item: self._tracks[item].seen_ms)
                    self._tracks.pop(oldest)
                    if oldest in visible_tracks:
                        self._offers.pop(oldest[1], None)
                track = self._tracks[track_key] = _Track()
            track.seen_ms = now_ms
            if row.kind == "emp_warning" and row.value is True and not _EMP.search(row.evidence):
                track.candidate, track.captures = None, set()
                continue

            if row.kind in _PERCENT:
                target = self._severity(track, _number(row.value), self._category(row.kind))
                candidate = ("percent", target)
            else:
                target = int(row.value)
                candidate = (("power", target) if row.kind == "power_online"
                             else ("boolean", target))
            if not self._confirmed(track, candidate, capture_id):
                continue
            track.candidate, track.captures = None, set()

            if row.kind == "power_online":
                was_on = track.known_power is True
                track.known_power = bool(target)
                if target == 1:
                    track.active = 0
                    continue
                if not was_on:
                    track.active = 0
                    continue
                target = 2
            elif row.kind in _BOOLEAN:
                if target == 0:
                    track.active = 0
                    continue
                target = 2 if row.kind in {"component_failed", "emp_warning", "fire_warning"} else 1

            previous = track.active
            if target == 0:
                track.active = 0
                continue
            track.active = target
            category = self._category(row.kind)
            if category in self.muted_categories or category in self.voice_muted:
                continue
            severity = self._severity_name(target)
            transition = previous == 0 or target > previous
            repeat_due = (
                track.last_attempt_ms is not None
                and track.last_attempt_severity == severity
                and now_ms - track.last_attempt_ms >= self.repeat_cooldown_ms
            )
            noted = self._external.get(self._category(row.kind))
            if (severity == "warning" and noted is not None
                    and 0 <= now_ms - noted < self.repeat_cooldown_ms):
                self._spent_externally(key, noted)
                continue
            escalation_unspoken = (
                severity == "critical" and track.last_attempt_severity == "warning"
            )
            if transition or track.last_attempt_ms is None or repeat_due or escalation_unspoken:
                notices.append(self._offer(row, key, target, captured_ms))
        return tuple(notices)

    def current(self, notice: Notice, now_ms: int) -> bool:
        """Recheck the exact latest offer immediately before speech."""
        if not isinstance(notice, Notice):
            self._clear_pending()
            return False
        return bool(self.current_burst((notice,), now_ms=now_ms))

    def current_burst(self, notices: Sequence[Notice], now_ms: int) -> tuple[Notice, ...]:
        """Return the still-current offers to speak together, most urgent first.

        One frame can raise several warnings in an intense fight. Speaking them
        one after another means later ones play stale, so they are folded into a
        single utterance of at most ``burst_limit`` members. Offers beyond the
        limit stay unspent and are offered again by a later frame; nothing queues.
        """
        now_ms = self._clock(now_ms, "now_ms")
        self._checked = ()
        if (type(notices) not in (tuple, list)
                or any(not isinstance(notice, Notice) for notice in notices)
                or self._last_now_ms is not None and now_ms < self._last_now_ms):
            self._clear_pending()
            return ()
        self._last_now_ms = now_ms
        live = [notice for index, notice in enumerate(notices)
                if self._offers.get(notice.key) is notice and now_ms < notice.expires_ms
                and all(notice is not other for other in notices[:index])]
        live.sort(key=lambda item: (item.severity != "critical", item.key))
        self._checked = tuple(live[:self.burst_limit])
        return self._checked

    def attempted(self, notice: Notice) -> None:
        """Spend cooldown only after the caller submits this current offer."""
        self.attempted_burst((notice,))

    def attempted_burst(self, notices: Sequence[Notice]) -> None:
        """Spend cooldown for each submitted member of the last checked burst."""
        if self._last_now_ms is None:
            return
        checked = self._checked
        for notice in notices:
            if (all(notice is not member for member in checked)
                    or self._offers.get(notice.key) is not notice):
                continue
            track = self._tracks.get(self._track_key(notice.key))
            if track is not None:
                track.last_attempt_ms = self._last_now_ms
                track.last_attempt_severity = notice.severity
        self._checked = ()


    def mute_for_now(self, category: str, muted: bool) -> None:
        """Silence or restore one category until unmuted or the skill reloads.

        Muting withdraws that category's pending offers without spending their
        repeat cooldown, so a condition that still holds is offered again soon
        after it is unmuted. Saved category switches are unchanged.
        """
        if type(category) is not str or category not in _CATEGORIES:
            raise ValueError("unknown warning category")
        if type(muted) is not bool:
            raise ValueError("muted must be true or false")
        if not muted:
            self.voice_muted = self.voice_muted - {category}
            return
        self.voice_muted = self.voice_muted | {category}
        for key, notice in list(self._offers.items()):
            if notice.category == category:
                del self._offers[key]
        self._checked = tuple(item for item in self._checked if item.category != category)

    def set_low_for_now(self, category: str, percent: float | None) -> tuple[float, float]:
        """Set one percentage category's low level until reset or reload.

        None restores the saved level. The critical level is unchanged, so the
        new low level may not be below it. Pending offers for the category are
        withdrawn without spending repeat cooldown and are re-judged against
        the new level on the next frame. Returns the effective (low, critical).
        """
        if type(category) is not str or category not in _PERCENT_CATEGORIES:
            raise ValueError("unknown percentage category")
        if percent is None:
            self.voice_low.pop(category, None)
        else:
            if type(percent) not in (int, float):
                raise ValueError("low level must be a number")
            level = _number(percent)
            if not self.levels(category)[1] <= level <= 100:
                raise ValueError("low level must be between the critical level and 100")
            self.voice_low[category] = level
        for key, notice in list(self._offers.items()):
            if notice.category == category:
                del self._offers[key]
        self._checked = tuple(item for item in self._checked if item.category != category)
        return self.levels(category)

    def availability(self, *, now_ms: int | None = None) -> dict[str, tuple[str, ...]]:
        """Which categories the latest accepted frame read and which are silent.

        "reading" lists categories present in the latest fresh, accepted frame;
        "off" lists categories switched off in saved settings; "muted" lists
        categories muted by voice for now. A frame older than the observation
        freshness window is unknown and contributes no reading categories.
        """
        silent = self.muted_categories | self.voice_muted
        current = self._last_now_ms if now_ms is None else self._clock(now_ms, "now_ms")
        fresh = (current is not None and self._last_capture_ms is not None
                 and current >= self._last_capture_ms
                 and current - self._last_capture_ms <= self.freshness_ms)
        reading = self._visible if fresh else frozenset()
        return {"reading": tuple(sorted(reading - silent)),
                "off": tuple(sorted(self.muted_categories)),
                "muted": tuple(sorted(self.voice_muted - self.muted_categories))}

    def lose_reading(self) -> None:
        """Withdraw unreadable state while retaining this scope's warning history."""
        self._change_ship_view(None)

    def reset(self) -> None:
        """Withdraw pending state and forget all measurements for this run."""
        self._reset_scope(None)
        self._last_now_ms = None


def fold_text(notices: Sequence[Notice]) -> str:
    """Join one burst into a single short utterance, preserving member order."""
    if not notices:
        raise ValueError("a burst needs at least one notice")
    return "; ".join(notice.text for notice in notices)
