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
PersonalStatus = Literal["low", "dangerously_low", "recovered"]
DEFAULT_CONFIRMATIONS = 2
MAX_CONFIRMATIONS = 64
_PERCENT = frozenset({
    "shield_charge", "component_health", "fuel_percent",
    "quantum_fuel_percent", "oxygen_percent", "hull_integrity_percent",
    "armor_percent", "suit_oxygen_percent",
})
_PERSONAL_STATUS = frozenset({"personal_hydration_status", "personal_nutrition_status"})
_PERSONAL_WARNINGS = frozenset({"personal_blood_loss_warning", "personal_high_pressure_warning",
                                "personal_incapacitated_warning"})
_PERSONAL_READINGS = _PERSONAL_STATUS | _PERSONAL_WARNINGS
_PERSONAL_STATUS_VALUES = frozenset({"low", "dangerously_low", "recovered"})
_COCKPIT_BANNERS = {
    "shields_critical_warning": ("Shields", "Shields critical"),
    "torque_imbalance_warning": ("Flight control", "Major torque imbalance"),
}
_HUD_WARNING_KINDS = frozenset(_COCKPIT_BANNERS) | {"smoke_warning"}
_BOOLEAN = _HUD_WARNING_KINDS | _PERSONAL_WARNINGS | frozenset({
    "power_online", "overheating", "component_failed",
    "environment_warning", "emp_warning", "fire_warning", "power_plant_distortion_warning",
    "reactor_explosion_imminent_warning",
})
_KINDS = _PERCENT | _BOOLEAN | _PERSONAL_STATUS
_PERCENT_CATEGORIES = frozenset({
    "shields", "components", "fuel", "quantum_fuel", "life_support", "hull", "armor", "suit_oxygen",
})
_PERSONAL_STATUS_CATEGORIES = frozenset({"hydration", "nutrition", "blood_loss", "pressure",
                                         "incapacitation"})
_CATEGORIES = (_PERCENT_CATEGORIES | _PERSONAL_STATUS_CATEGORIES
               | frozenset({"power", "heat", "environment", "emp", "fire"}))
_MAX_TEXT = 96_000
_MAX_ROWS = 128
_MAX_TRACKS = 128
_MAX_MS = 2**63 - 1
_WARNING_REFERENCE_MS = 15_000
_WS = re.compile(r"\s+")
_LABEL = re.compile(r"[^A-Za-z0-9 _-]+", re.ASCII)
_EMP = re.compile(r"(?<![A-Za-z0-9])EMP(?![A-Za-z0-9])", re.IGNORECASE)
_DISTORTION_METRIC = re.compile(r"\bpower\s+plant\s+distortion\b", re.I | re.ASCII)
_DISTORTION_BANNER = re.compile(
    r"warning(?:\s*:\s*|\s+)power plant distortion"
    r"(?:\s+(?:100(?:\.0+)?|(?:0|[1-9][0-9]?)(?:\.[0-9]+)?)%)?[.!]?", re.I | re.ASCII,
)
_REACTOR_KEY = "reactor_explosion_imminent_warning:reactor"
_SHIELDS_CRITICAL_KEY = "shields_critical_warning:shields"
_REACTOR_EXPLOSION_BANNER = re.compile(
    r"reactor critical:? explosion imminent(?:\s+[0-9]+(?:\s+seconds?|\s*s))?[.!]?",
    re.I | re.ASCII,
)
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
_FUEL_IDENTITIES = {"fuel_percent": _HYDROGEN_FUEL, "quantum_fuel_percent": _QUANTUM_FUEL}
_LITERAL_PERCENT = r"(?<![0-9.])[0-9]+(?:\.[0-9]+)?%"
_FUEL_VALUE_PAIRS = {
    kind: re.compile(
        rf"(?:{identity.pattern}(?:\s*:\s*|[ \t]+){_LITERAL_PERCENT}|{_LITERAL_PERCENT}[ \t]+{identity.pattern})",
        re.I | re.ASCII,
    )
    for kind, identity in _FUEL_IDENTITIES.items()
}
_EMPTY_FUEL_GAUGE = re.compile(r"(hydrogen fuel|quantum fuel)(?:\s*:\s*|\s+)empty", re.I)
_TERMINAL_PERCENT_METRICS = {
    "oxygen_percent": r"(?:oxygen|o2)",
    "component_health": r"(?:health|integrity)",
}
_SHIP_OXYGEN_METRIC = r"(?:ship|cabin|cockpit|room|interior)\s+(?:oxygen|o2)"
_SUIT_OXYGEN_GAUGE = r"(?:suit\s+(?:oxygen|o2|oxg)|(?:helmet|personal)\s+(?:oxygen|o2))"
_PERSONAL_STATUS_EVIDENCE = {
    "personal_hydration_status": {
        "low": "caution! hydration levels low",
        "dangerously_low": "warning! hydration levels dangerously low",
        "recovered": "optimal hydration level achieved",
    },
    "personal_nutrition_status": {
        "low": "caution! nutrition levels low",
        "dangerously_low": "warning! nutrition levels dangerously low",
        "recovered": "optimal nutrition level achieved",
    },
}
_PERSONAL_WARNING_EVIDENCE = {
    "personal_blood_loss_warning": ("blood_loss", "blood loss", "blood loss"),
    "personal_incapacitated_warning": ("incapacitation", "incapacitation", "you are incapacitated"),
    "personal_high_pressure_warning": ("pressure", "pressure", "warning high pressure!"),
}
_PERSONAL_HUD_ALERTS = {
    _WS.sub(" ", evidence).casefold(): (kind, _PERSONAL_WARNING_EVIDENCE[kind][1], evidence)
    for kind, (_category, _label, evidence) in _PERSONAL_WARNING_EVIDENCE.items()
}
_MAX_HUD_ALERTS = 32
_NON_SHIP_OXYGEN = re.compile(
    r"\b(?:suit|helmet|personal|ambient|external|outside|target)\b", re.I | re.ASCII,
)
_COMPONENT_STATE_KINDS = frozenset({
    "power_online", "component_failed", "component_health", "overheating",
})
_PAGE_HEADINGS = frozenset({"output", "power management", "power management output"})
# Bare HUD percentages have no panel context. A hardware class can identify a
# compact gauge; a labelled component or selected console can identify any name.
_HARDWARE_GAUGE = re.compile(
    r"(?:(?:port|starboard|left|right|fore|front|forward|aft|rear|upper|lower|"
    r"top|bottom|main|primary|secondary|backup|auxiliary|aux)\s+)*"
    r"(?:power\s?plant|shield (?:generator|emitter)|cooler|reactor(?: core)?|"
    r"quantum drive|jump drive|engine|thruster|gravity generator|life support|"
    r"radar|sensor|battery|relay|fuse|capacitor)(?:[ #_-]*[0-9]+)?",
    re.I | re.ASCII,
)
_DIRECTIONAL_SHIELD = re.compile(r"\bdirectional\s+shields?\b", re.IGNORECASE | re.ASCII)
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
_SHIELD_FACE_LABEL = rf"{_SHIELD_DIRECTION.pattern}\s+shields?"
_SHIELD_FACE_PERCENT = re.compile(
    rf"(?:{_SHIELD_FACE_LABEL}(?:\s*:\s*|\s+)[0-9.]+%|"
    rf"[0-9.]+%\s+{_SHIELD_FACE_LABEL})[.]?", re.I | re.ASCII,
)
_SHIELD_FACE_NAME = re.compile(_SHIELD_FACE_LABEL, re.I | re.ASCII)
_SHIELD_GAUGE = r"(?:shields?|shld)(?:\s+(?:charge|strength))?"
_DAMAGE_GAUGES = {
    "hull_integrity_percent": r"(?:ship\s+)?hull(?:\s+(?:integrity|health))?",
    "armor_percent": r"(?:ship\s+)?armou?r(?:\s+(?:remaining|integrity|health))?",
    "shield_charge": rf"(?:ship\s+)?{_SHIELD_GAUGE}",
}
_DAMAGE_VALUE_PAIRS = {
    kind: re.compile(
        rf"(?:{gauge}(?:\s*[:=-]\s*|\s+){_LITERAL_PERCENT}|{_LITERAL_PERCENT}\s+{gauge})[.]?",
        re.I | re.ASCII,
    )
    for kind, gauge in _DAMAGE_GAUGES.items()
}
_FACE_GAUGE = (
    rf"(?:{_SHIELD_DIRECTION.pattern}(?:\s+{_SHIELD_GAUGE})?"
    rf"|{_SHIELD_GAUGE}\s+{_SHIELD_DIRECTION.pattern})"
)
_FACE_VALUE_PAIR = re.compile(
    rf"(?:directional\s+shields?\s*:\s*)?"
    rf"(?:{_FACE_GAUGE}(?:\s*[:=-]\s*|\s+){_LITERAL_PERCENT}|{_LITERAL_PERCENT}\s+{_FACE_GAUGE})[.]?",
    re.I | re.ASCII,
)


def _shield_faces(text: str) -> set[str]:
    return {_SHIELD_DIRECTIONS[word.casefold()] for word in _SHIELD_DIRECTION.findall(text)}


def _shield_face_key(label: str) -> str | None:
    if re.fullmatch(_FACE_GAUGE, label.strip(), re.I | re.ASCII) is None:
        return None
    face, = _shield_faces(label)
    return f"{face} shields"


_POWER_STATES = {True: ("online", "on", "enabled"), False: ("offline", "off", "disabled")}
_COMPONENT_STATUS = {
    "power_online": _POWER_STATES,
    "component_failed": {
        True: ("failed", "failure", "critical failure", "destroyed", "inoperable", "broken"),
        False: ("operational", "no failure", "failure cleared"),
    },
    "overheating": {
        True: ("overheating", "overheat", "overheated", "overheat warning",
               "temperature warning"),
        False: ("temperature normal", "no overheat", "overheat cleared"),
    },
}
_HAZARD_STATES = {
    "fire_warning": {
        True: ("fire", "fire warning", "fire detected", "fire active", "active fire", "on fire"),
        False: ("no fire", "no fire detected", "fire extinguished", "fire cleared"),
    },
    "emp_warning": {
        True: ("emp warning", "emp detected", "emp incoming", "incoming emp", "emp active",
               "emp detected incoming", "emp impact", "emp hit"),
        False: ("no emp detected", "emp cleared", "emp effect cleared"),
    },
}
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
    value: float | bool | PersonalStatus
    evidence: str
    source: str = "vision_model"


@dataclass(frozen=True, slots=True)
class Notice:
    key: str
    category: str
    severity: Severity
    text: str
    expires_ms: int


SCENES = ("cockpit", "ship_exterior", "engineering_terminal", "ground_vehicle", "fps", "menu_or_map",
          "cutscene_or_loading", "unknown")
# A terminal is eligible only for identifiable current ship-system readings.
SHIP_SCENES = frozenset({"cockpit", "ship_exterior", "engineering_terminal"})
VEHICLE_SCENES = SHIP_SCENES | {"ground_vehicle"}
_GROUND_READINGS = frozenset({
    "shield_charge", "shields_critical_warning", "armor_percent", "hull_integrity_percent",
    "component_health", "component_failed", "power_online", "overheating",
    "fuel_percent", "oxygen_percent", "fire_warning", "emp_warning", "environment_warning",
})


def scene_admits_kind(scene: str, kind: str) -> bool:
    if scene == "fps":
        return kind == "suit_oxygen_percent" or kind in _PERSONAL_READINGS
    if scene == "ground_vehicle":
        return kind in _GROUND_READINGS
    return scene in SHIP_SCENES and kind not in ({"suit_oxygen_percent"} | _PERSONAL_READINGS)


_SHIP_OVERVIEW_PAGE = "3d view"
_STATUS_STRIP = "engineering status strip"
_FAILURE_STRIP = "engineering component failure strip"
_STATUS_LABELS = frozenset({"armor", "hull", "cooling system", "life support", "hydrogen fuel"})
_STATUS_ROW_LABELS = _STATUS_LABELS | {"nav", "scm"}
_SHIP_OVERVIEW_CUES = frozenset({"systems", "rooms", "connections"})
_SHIP_OVERVIEW_READINGS = frozenset({"fuel_percent", "quantum_fuel_percent",
                                    "hull_integrity_percent", "armor_percent"})


@dataclass(frozen=True, slots=True)
class TerminalView:
    page: str
    subject: str | None

    def __post_init__(self) -> None:
        for key in ("page", "subject"):
            value = getattr(self, key)
            if key == "subject" and value is None and self.page in {_SHIP_OVERVIEW_PAGE, _STATUS_STRIP}:
                continue
            token = _WS.sub(" ", _text(value, key, 80)).casefold()
            if token in {"unknown", "none", "unreadable", "n/a"} or not any(
                    character.isalnum() for character in token):
                raise ValueError("terminal identity must be visible text")
            object.__setattr__(self, key, token)

    def admits(self, kind: str, reading: Reading | None = None) -> bool:
        if self.page == _FAILURE_STRIP or (self.subject is None and kind == "component_failed"):
            if reading is None or kind != "component_failed" or reading.value is not True:
                return False
            part = _failure_strip_subject(reading.evidence)
            return (part is not None and part == _WS.sub(" ", reading.label.strip()).casefold()
                    and (self.subject is None or self.subject == part))
        if self.page == _STATUS_STRIP and self.subject is None:
            return kind == "reactor_explosion_imminent_warning"
        return (self.subject is not None or kind in _SHIP_OVERVIEW_READINGS
                or kind == "reactor_explosion_imminent_warning")


def _failure_strip_subject(banner: object) -> str | None:
    if type(banner) is not str:
        return None
    name, suffix, tail = _WS.sub(" ", banner.strip()).casefold().partition(" failure")
    return name if suffix and not tail and _HARDWARE_GAUGE.fullmatch(name) else None


def _has_ship_status_labels(labels: object) -> bool:
    if (type(labels) is not list or not 2 <= len(labels) <= len(_STATUS_ROW_LABELS)
            or not all(type(label) is str for label in labels)):
        return False
    distinct = {label.strip().casefold() for label in labels}
    # NAV and SCM can appear beside the gauges. Only distinct gauge names identify the strip.
    return (len(distinct) == len(labels) and distinct <= _STATUS_ROW_LABELS
            and len(distinct & _STATUS_LABELS) >= 2)


def _terminal_view(payload: object) -> TerminalView | None:
    if type(payload) is not dict:
        return None
    try:
        if set(payload) == {"page", "subject"} and payload["subject"] is not None:
            view = TerminalView(page=payload["page"], subject=payload["subject"])
            return view if view.page != _FAILURE_STRIP else None
        if set(payload) == {"page", "overview_cues"}:
            cues = payload["overview_cues"]
            if (type(payload["page"]) is str and payload["page"].strip().casefold() == _SHIP_OVERVIEW_PAGE
                    and type(cues) is list and len(cues) == len(_SHIP_OVERVIEW_CUES)
                    and all(type(cue) is str for cue in cues)
                    and {cue.strip().casefold() for cue in cues} == _SHIP_OVERVIEW_CUES):
                return TerminalView(page=payload["page"], subject=None)
        if set(payload) == {"status_labels", "active_banner"} and _has_ship_status_labels(payload["status_labels"]):
            part = _failure_strip_subject(payload["active_banner"])
            if part is not None:
                return TerminalView(page=_FAILURE_STRIP, subject=part)
        elif set(payload) == {"status_labels", "filter_heading", "highlighted_filter"}:
            labels, heading, active = (payload["status_labels"], payload["filter_heading"],
                                       payload["highlighted_filter"])
            if (_has_ship_status_labels(labels)
                    and type(heading) is str and heading.strip().casefold() == "filters"
                    and type(active) is str and active.strip().casefold() in _SHIP_OVERVIEW_CUES):
                return TerminalView(page=_STATUS_STRIP, subject=None)
    except ValueError:
        return None
    return None


def _visual_envelope(text: str) -> tuple[list[object], str, TerminalView | None, list[object]]:
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
            or not set(payload) <= {"readings", "scene", "terminal", "hud_alerts"}):
        raise ValueError("visual JSON has unknown keys or missing readings")
    rows = payload["readings"]
    if type(rows) is not list or len(rows) > _MAX_ROWS:
        raise ValueError("readings must be a bounded list")
    hud_alerts = payload.get("hud_alerts", [])
    if type(hud_alerts) is not list or len(hud_alerts) > _MAX_HUD_ALERTS:
        raise ValueError("hud_alerts must be a bounded list")
    scene = payload.get("scene")
    scene = scene.strip().casefold() if type(scene) is str else "unknown"
    terminal = _terminal_view(payload.get("terminal")) if scene == "engineering_terminal" else None
    return rows, scene if scene in SCENES else "unknown", terminal, hud_alerts


def _visual_rows(text: str) -> list[object]:
    return _visual_envelope(text)[0]


def _matches_hazard(kind: str, value: bool, label: str, evidence: str) -> bool:
    name = _WS.sub(" ", label).casefold()
    text = _WS.sub(" ", evidence).casefold().rstrip(".! ")
    states = _HAZARD_STATES[kind][value]
    if kind == "fire_warning" and value and text == f"fire in {name}":
        return True
    subject, colon, status = text.partition(":")
    if colon:
        return subject.strip() == name and status.strip() in states
    return text in states or any(text == f"{name} {state}" for state in states)


def _matches_component_status(kind: str, value: bool, label: str, evidence: str) -> bool:
    name = re.escape(_WS.sub(" ", label).casefold())
    states = "|".join(_COMPONENT_STATUS[kind][value])
    pair = rf"(?:{name}(?: *: *| +)(?:{states})|(?:{states}) +{name})[.]?"
    return re.fullmatch(pair, _WS.sub(" ", evidence).casefold()) is not None


def _matches_component_health(label: str, evidence: str, terminal: TerminalView | None) -> bool:
    name = _WS.sub(" ", label).casefold()
    quote = _WS.sub(" ", evidence).casefold()
    boundary = r"(?:\s*[:=]\s*|\s+)"
    subject = re.escape(name)
    metric = rf"(?:health|integrity){boundary}{_LITERAL_PERCENT}"
    # A component label on the display also works for future hardware.
    if re.fullmatch(rf"component{boundary}{subject}{boundary}{metric}[.]?", quote, re.ASCII):
        return True
    hardware_gauge = _HARDWARE_GAUGE.fullmatch(name) is not None
    if not hardware_gauge and not (terminal and terminal.subject == name):
        return False
    if terminal and re.fullmatch(rf"{metric}[.]?", quote, re.ASCII):
        # A named hardware detail page binds a bare HEALTH gauge to that part.
        # Room views can instead contain rows for multiple named components.
        return (terminal.subject == name or
                (terminal.subject is not None and not _HARDWARE_GAUGE.fullmatch(terminal.subject)))
    return re.fullmatch(rf"{subject}{boundary}(?:{metric}|{_LITERAL_PERCENT})[.]?", quote, re.ASCII) is not None


def _parse_row(row: object, *, scene: str, terminal: TerminalView | None) -> Reading:
    if type(row) is not dict or set(row) != {"kind", "label", "value", "evidence"}:
        raise ValueError("reading has missing or unknown keys")
    kind = row["kind"]
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("unsupported reading kind")
    label = _text(row["label"], "label", 80)
    evidence = _text(row["evidence"], "evidence", 512)
    value = row["value"]
    if kind in _PERSONAL_STATUS:
        category = "hydration" if kind == "personal_hydration_status" else "nutrition"
        quote = _WS.sub(" ", evidence).casefold()
        if (scene != "fps" or _WS.sub(" ", label).casefold() != category
                or type(value) is not str or value not in _PERSONAL_STATUS_VALUES
                or quote != _PERSONAL_STATUS_EVIDENCE[kind][value]):
            raise ValueError("personal status needs its exact player HUD message on foot")
        return Reading(kind, label, value, evidence)
    if kind in _PERSONAL_WARNINGS:
        _, label_name, literal = _PERSONAL_WARNING_EVIDENCE[kind]
        if (scene != "fps" or _WS.sub(" ", label).casefold() != label_name
                or value is not True
                or _WS.sub(" ", evidence).casefold() != literal):
            raise ValueError("personal warning needs its exact player HUD alert on foot")
        return Reading(kind, label, value, evidence)
    if scene == "ground_vehicle" and not scene_admits_kind(scene, kind):
        raise ValueError("reading does not describe this ground vehicle")
    if kind == "component_failed" and scene == "engineering_terminal" and terminal is None:
        raise ValueError("component failure needs an identified engineering display")
    if kind in _HUD_WARNING_KINDS:
        quote = _WS.sub(" ", evidence).casefold().rstrip(".!")
        name = _WS.sub(" ", label).casefold()
        if kind == "smoke_warning":
            if (not re.fullmatch(r"[a-z0-9][a-z0-9 '-]*", name)
                    or re.search(r"\b(?:cleared|resolved|ended|all clear)$", name)):
                raise ValueError("smoke warning needs its visible room name")
            message = f"smoke detected in {name}"
        else:
            subject, message = _COCKPIT_BANNERS[kind]
            if name != subject.casefold():
                raise ValueError("warning banner does not identify that subject")
        if (scene not in {"cockpit", "ground_vehicle"} or value is not True
                or not re.fullmatch(r"warning\s*:\s*" + re.escape(message.casefold()), quote)):
            raise ValueError("cockpit warning needs its complete visible HUD banner")
    elif kind == "power_plant_distortion_warning":
        quote = _WS.sub(" ", evidence).casefold()
        if (scene not in SHIP_SCENES or _WS.sub(" ", label).casefold() != "power plant"
                or type(value) is not bool
                or (value and not _DISTORTION_BANNER.fullmatch(quote))
                or (not value and quote.rstrip(".!") != "power plant distortion cleared")):
            raise ValueError("power plant distortion needs its own visible warning or clear status")
    elif kind == "reactor_explosion_imminent_warning":
        quote = _WS.sub(" ", evidence).casefold()
        if ((scene != "cockpit" and (scene != "engineering_terminal" or terminal is None))
                or _WS.sub(" ", label).casefold() != "reactor"
                or type(value) is not bool or value is not True
                or _REACTOR_EXPLOSION_BANNER.fullmatch(quote) is None):
            raise ValueError("reactor explosion warning needs its literal visible alert")
    elif _DISTORTION_METRIC.search(evidence) or _DISTORTION_METRIC.search(label):
        raise ValueError("power plant distortion does not establish another system reading")
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
            metric = (
                _TERMINAL_PERCENT_METRICS.get(kind) if scene == "engineering_terminal" else None
            )
            if kind == "oxygen_percent" and scene in VEHICLE_SCENES:
                if _NON_SHIP_OXYGEN.search(f"{label} {evidence}"):
                    raise ValueError("suit or ambient oxygen is not ship oxygen")
                if scene != "engineering_terminal":
                    metric = _SHIP_OXYGEN_METRIC
            if gauge and scene in VEHICLE_SCENES and (
                _FUEL_IDENTITIES[kind].fullmatch(label) is None
                or _FUEL_VALUE_PAIRS[kind].search(evidence) is None
            ):
                raise ValueError("fuel evidence must pair the named gauge and percentage")
            if metric is not None:
                amount = r"[0-9]+(?:\.[0-9]+)?%"
                pair = rf"(?:\b{metric}(?:\s*:\s*|\s+){amount}|{amount}\s+{metric}\b)"
                if re.search(pair, evidence, re.I | re.ASCII) is None:
                    raise ValueError("visible evidence must pair the metric and percentage")
        if (kind in {"shield_charge", "hull_integrity_percent", "armor_percent"}
                and _HUD_RESOURCE_PERCENT.search(evidence)):
            raise ValueError("HUD resource percentage does not establish this reading")
        if kind == "component_health" and (
            _SHIELD_FACE_NAME.fullmatch(label) or _SHIELD_FACE_PERCENT.fullmatch(evidence)
        ):
            raise ValueError("shield face charge is not component health")
        if kind == "component_health" and not _matches_component_health(label, evidence, terminal):
            raise ValueError("health evidence must identify onboard hardware and its own gauge")
        if kind == "suit_oxygen_percent":
            pair = rf"(?:{_SUIT_OXYGEN_GAUGE}(?:\s*:\s*|\s+){_LITERAL_PERCENT}|{_LITERAL_PERCENT}\s+{_SUIT_OXYGEN_GAUGE})[.]?"
            if (scene != "fps" or re.fullmatch(_SUIT_OXYGEN_GAUGE, label, re.I | re.ASCII) is None
                    or re.fullmatch(pair, evidence, re.I | re.ASCII) is None):
                raise ValueError("suit oxygen needs an explicit personal gauge on foot")
        directional = False
        if kind == "shield_charge":
            label_faces = _shield_faces(label)
            evidence_faces = _shield_faces(evidence)
            directional = (label_faces or evidence_faces or _DIRECTIONAL_SHIELD.search(label)
                           or _DIRECTIONAL_SHIELD.search(evidence))
            if directional and (len(label_faces) != 1 or label_faces != evidence_faces):
                raise ValueError("directional shield evidence must match one visible face")
        if kind in _DAMAGE_GAUGES:
            metric = _FACE_GAUGE if directional else _DAMAGE_GAUGES[kind]
            pair = _FACE_VALUE_PAIR if directional else _DAMAGE_VALUE_PAIRS[kind]
            quote = evidence
            if len(mentions) > 1:
                # Identical repeats are permitted by the numeric check above.
                quote = evidence[:next(_PERCENT_MENTION.finditer(evidence)).end()]
            if re.fullmatch(metric, label, re.I | re.ASCII) is None or pair.fullmatch(quote) is None:
                raise ValueError("damage evidence must pair the visible gauge and its percentage")
        if (kind == "fuel_percent" and _QUANTUM_FUEL.search(label + " " + evidence)
                or kind == "quantum_fuel_percent"
                and _HYDROGEN_FUEL.search(label + " " + evidence)):
            raise ValueError("explicit fuel identity contradicts the reading category")
    elif (kind == "component_failed" and label.casefold() == "battery" and type(value) is str
          and value.casefold() == "offline"
          and _matches_component_status("power_online", False, label, evidence)):
        # BATTERY identifies a power system even when a reader mislabels OFFLINE.
        kind, value = "power_online", False
    elif type(value) is not bool:
        raise ValueError("boolean reading must be true or false")
    if kind in _HAZARD_STATES and not _matches_hazard(kind, value, label, evidence):
        raise ValueError("hazard evidence must show the stated active or clear status")
    if (kind in _COMPONENT_STATE_KINDS
            and _WS.sub(" ", label).casefold() in _PAGE_HEADINGS):
        raise ValueError("page heading is not a component identity")
    if kind in _COMPONENT_STATUS and not _matches_component_status(kind, value, label, evidence):
        raise ValueError("evidence must pair the component with its explicit state")
    return Reading(kind, label, value, evidence)


def _reading_label_key(kind: str, label: str) -> str:
    normalized = _WS.sub(" ", label.strip()).casefold()
    if kind == "suit_oxygen_percent":
        return "suit oxygen"
    if kind == "shield_charge":
        if re.fullmatch(_DAMAGE_GAUGES[kind], normalized, re.ASCII):
            return "shields"
        return _shield_face_key(normalized) or normalized
    return normalized


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
    key = _reading_label_key(kind, label)
    evidence = row.get("evidence")
    if (kind == "shield_charge" and key == "shields" and type(evidence) is str
            and (_shield_faces(evidence) or _DIRECTIONAL_SHIELD.search(evidence))):
        # A rejected face quote cannot conflict with the independently quoted total.
        return None
    return kind, key


@dataclass(frozen=True, slots=True)
class VisualParseReport:
    readings: tuple[Reading, ...]
    rejected_count: int
    scene: str = "unknown"
    terminal: TerminalView | None = None
    rejection_reasons: dict[str, int] = field(default_factory=dict, compare=False, hash=False)


# Only these code-owned labels enter diagnostics; never expose a row or an
# arbitrary exception message. Future validation failures retain a safe fallback.
_ROW_REJECTION_REASONS = {
    "reading does not describe this ground vehicle": "unrelated_vehicle_reading",
    "reading has missing or unknown keys": "invalid_shape",
    "unsupported reading kind": "unsupported_kind",
    "label must be text": "invalid_label",
    "invalid label": "invalid_label",
    "evidence must be text": "invalid_evidence",
    "invalid evidence": "invalid_evidence",
    "number must be finite": "nonfinite_percentage",
    "component failure needs an identified engineering display": "unidentified_terminal",
    "smoke warning needs its visible room name": "unidentified_room",
    "warning banner does not identify that subject": "warning_subject_mismatch",
    "cockpit warning needs its complete visible HUD banner": "invalid_cockpit_banner",
    "power plant distortion needs its own visible warning or clear status": "invalid_distortion_warning",
    "reactor explosion warning needs its literal visible alert": "invalid_reactor_warning",
    "power plant distortion does not establish another system reading": "wrong_distortion_metric",
    "percentage must be numeric": "non_numeric_percentage",
    "percentage must be between 0 and 100": "percentage_out_of_range",
    "empty fuel evidence must match the named gauge at zero": "empty_fuel_mismatch",
    "percentage evidence must contain a literal decimal percentage": "missing_percentage",
    "percentage value must match one unambiguous literal percentage": "percentage_value_mismatch",
    "suit or ambient oxygen is not ship oxygen": "non_ship_oxygen",
    "fuel evidence must pair the named gauge and percentage": "unidentified_fuel_gauge",
    "visible evidence must pair the metric and percentage": "missing_metric",
    "HUD resource percentage does not establish this reading": "hud_resource_mismatch",
    "shield face charge is not component health": "shield_not_component_health",
    "health evidence must identify onboard hardware and its own gauge": "unidentified_component_health",
    "directional shield evidence must match one visible face": "shield_face_mismatch",
    "damage evidence must pair the visible gauge and its percentage": "damage_gauge_mismatch",
    "explicit fuel identity contradicts the reading category": "fuel_kind_mismatch",
    "boolean reading must be true or false": "non_boolean_status",
    "hazard evidence must show the stated active or clear status": "hazard_status_mismatch",
    "page heading is not a component identity": "page_heading_not_component",
    "evidence must pair the component with its explicit state": "component_status_mismatch",
    "reading is not identified by this engineering view": "terminal_scope_mismatch",
    "suit oxygen needs an explicit personal gauge on foot": "unidentified_suit_oxygen",
    "personal status needs its exact player HUD message on foot": "unidentified_personal_status",
    "personal warning needs its exact player HUD alert on foot": "unidentified_personal_warning",
}
REJECTION_REASONS = frozenset(_ROW_REJECTION_REASONS.values()) | {
    "conflicting_readings", "conflict_suppressed", "invalid_reading",
}


def parse_visual_report(text: str) -> VisualParseReport:
    """Keep valid independent readings; reject every participant in a conflict."""
    rows, scene, terminal, hud_alerts = _visual_envelope(text)
    if scene == "fps":
        for alert in hud_alerts:
            if type(alert) is not str or len(alert) > 512:
                continue
            normalized = _WS.sub(" ", alert.strip()).casefold()
            matched = _PERSONAL_HUD_ALERTS.get(normalized)
            if matched is not None:
                kind, label, _literal = matched
                rows.append({"kind": kind, "label": label, "value": True, "evidence": alert})
    groups: dict[tuple[str, str], list[tuple[Reading | None, str | None]]] = {}
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    unkeyed = 0
    for row in rows:
        identity = _identity(row)
        reason = None
        try:
            reading = _parse_row(row, scene=scene, terminal=terminal)
            if terminal is not None and not terminal.admits(reading.kind, reading):
                raise ValueError("reading is not identified by this engineering view")
            identity = reading.kind, _reading_label_key(reading.kind, reading.label)
        except ValueError as exc:
            reading = None
            reason = _ROW_REJECTION_REASONS.get(str(exc), "invalid_reading")
        if identity is None:
            unkeyed += 1
            reject(reason or "invalid_reading")
        else:
            groups.setdefault(identity, []).append((reading, reason))
    accepted = []
    rejected = unkeyed
    for (kind, _label), entries in groups.items():
        readings = [reading for reading, _reason in entries]
        first = readings[0]
        if first is None or any(
            item is None or item.value != first.value
            or (kind != "shield_charge" and item.evidence.casefold() != first.evidence.casefold())
            for item in readings
        ):
            rejected += len(readings)
            conflict = ("conflict_suppressed" if any(item is None for item in readings)
                        else "conflicting_readings")
            for _reading, reason in entries:
                reject(reason or conflict)
        else:
            accepted.append(first)
    return VisualParseReport(tuple(accepted), rejected, scene, terminal, dict(sorted(reasons.items())))


def parse_visual(text: str) -> tuple[Reading, ...]:
    """Strict default: reject any invalid or conflicting reading."""
    report = parse_visual_report(text)
    if report.rejected_count:
        raise ValueError("invalid or conflicting visual reading")
    return report.readings


@dataclass(slots=True)
class _Track:
    active: int = 0
    candidate: tuple[str, int | str] | None = None
    captures: set[str] = field(default_factory=set)
    known_power: bool | None = None
    power_loss_pending: bool = False
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
        personal_status_categories: set[str] | frozenset[str] | None = None,
        muted_categories: set[str] | frozenset[str] | None = None,
        confirmations: int = DEFAULT_CONFIRMATIONS,
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
        if personal_status_categories is None:
            personal_status: frozenset[str] = frozenset()
        else:
            if (type(personal_status_categories) not in (set, frozenset)
                    or any(type(category) is not str
                           or category not in _PERSONAL_STATUS_CATEGORIES
                           for category in personal_status_categories)):
                raise ValueError("personal_status_categories contains an unknown category")
            personal_status = frozenset(personal_status_categories)
        if muted_categories is None:
            muted: frozenset[str] = frozenset()
        else:
            if type(muted_categories) not in (set, frozenset):
                raise ValueError("muted_categories must be a set of categories")
            if any(type(category) is not str or category not in _CATEGORIES
                   for category in muted_categories):
                raise ValueError("muted_categories contains an unknown category")
            muted = frozenset(muted_categories)
        if type(confirmations) is not int or not 1 <= confirmations <= MAX_CONFIRMATIONS:
            raise ValueError(f"confirmations must be between 1 and {MAX_CONFIRMATIONS}")
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
        self.personal_status_categories = personal_status
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
        self._last_capture_observed_ms: int | None = None
        self._last_now_ms: int | None = None
        self._checked: tuple[Notice, ...] = ()
        self._external: dict[str, int] = {}
        self._visible: frozenset[str] = frozenset()
        self._visible_low_shield_faces: frozenset[str] = frozenset()
        self._last_spoken_shield: tuple[str, int] | None = None
        self._reactor_last_attempt_ms: int | None = None

    @staticmethod
    def _clock(value: object, name: str) -> int:
        if type(value) is not int or not 0 <= value <= _MAX_MS:
            raise ValueError(f"{name} must be a nonnegative millisecond timestamp")
        return value

    @staticmethod
    def _key(reading: Reading) -> str:
        return f"{reading.kind}:{_reading_label_key(reading.kind, reading.label)}"

    @staticmethod
    def _label(value: str) -> str:
        value = _WS.sub(" ", _LABEL.sub("", value)).strip(" _-")[:48]
        return value.title() if value else "System"

    @staticmethod
    def _category(kind: str) -> str:
        return {
            "shield_charge": "shields", "shields_critical_warning": "shields",
            "component_health": "components", "torque_imbalance_warning": "components",
            "fuel_percent": "fuel", "quantum_fuel_percent": "quantum_fuel",
            "oxygen_percent": "life_support", "suit_oxygen_percent": "suit_oxygen", "power_online": "power",
            "power_plant_distortion_warning": "power",
            "hull_integrity_percent": "hull", "armor_percent": "armor",
            "overheating": "heat", "component_failed": "components",
            "environment_warning": "environment", "emp_warning": "emp",
            "fire_warning": "fire", "smoke_warning": "fire",
            "reactor_explosion_imminent_warning": "power",
            "personal_hydration_status": "hydration",
            "personal_nutrition_status": "nutrition",
            **{kind: category for kind, (category, _, _) in _PERSONAL_WARNING_EVIDENCE.items()},
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
        elif row.kind in _PERSONAL_STATUS:
            category = "hydration" if row.kind == "personal_hydration_status" else "nutrition"
            quote = _WS.sub(" ", row.evidence).casefold()
            if (type(row.value) is not str or row.value not in _PERSONAL_STATUS_VALUES
                    or _WS.sub(" ", row.label).casefold() != category
                    or quote != _PERSONAL_STATUS_EVIDENCE[row.kind][row.value]):
                raise ValueError("invalid personal status")
        elif row.kind in _PERSONAL_WARNINGS:
            _, label_name, literal = _PERSONAL_WARNING_EVIDENCE[row.kind]
            if (row.value is not True or _WS.sub(" ", row.label).casefold() != label_name
                    or _WS.sub(" ", row.evidence).casefold() != literal):
                raise ValueError("invalid personal warning")
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
        label = "Suit oxygen" if row.kind == "suit_oxygen_percent" else self._label(row.label)
        if row.kind in _PERSONAL_STATUS:
            descriptor = "dangerously low" if row.value == "dangerously_low" else "low"
            text = f"{label} {descriptor}"
        elif row.kind in _PERSONAL_WARNINGS:
            text = {
                "personal_blood_loss_warning": "Blood loss detected",
                "personal_high_pressure_warning": "High pressure warning",
                "personal_incapacitated_warning": "You are incapacitated",
            }[row.kind]
        elif row.kind in _PERCENT:
            value = float(row.value)
            if row.kind == "shield_charge":
                face_label = _shield_face_key(row.label)
                shield = face_label.capitalize() if face_label else "Shields"
                text = f"{shield} depleted" if value == 0 else (
                    f"{shield} {'critically low' if level == 2 else 'low'} ({value:g}%)")
            else:
                text = f"{label} {'critically low' if level == 2 else 'low'} ({value:g}%)"
        elif row.kind in _COCKPIT_BANNERS:
            text = _COCKPIT_BANNERS[row.kind][1]
        else:
            text = {
                "power_online": f"Power offline: {label}",
                "power_plant_distortion_warning": "Power plant distortion warning",
                "overheating": f"{label} overheating",
                "component_failed": f"{label} failed",
                "environment_warning": f"Environment warning: {label}",
                "emp_warning": f"EMP warning: {label}",
                "fire_warning": f"Fire: {label}",
                "reactor_explosion_imminent_warning": "Reactor explosion imminent",
                "smoke_warning": f"Smoke detected in {label}",
            }[row.kind]
        notice = Notice(key, self._category(row.kind), self._severity_name(level), text,
                        min(captured_ms + self.freshness_ms, _MAX_MS))
        self._offers[key] = notice
        return notice

    def _clear_pending(self, *, keep_reactor_confirmation: bool = False) -> None:
        self._offers.clear()
        self._checked = ()
        self._visible = self._visible_low_shield_faces = frozenset()
        self._last_spoken_shield = None
        for (_, key), track in self._tracks.items():
            if keep_reactor_confirmation and key == _REACTOR_KEY:
                continue
            track.candidate, track.captures = None, set()

    def _reset_scope(self, scope: object | None) -> None:
        self._scope, self._tracks = scope, {}
        self._ship_view = None
        self._offers.clear()
        self._seen.clear()
        self._last_capture_ms = None
        self._last_capture_observed_ms = None
        self._checked = ()
        self._external.clear()
        self._visible = self._visible_low_shield_faces = frozenset()
        self._last_spoken_shield = None
        self._reactor_last_attempt_ms = None

    def _change_ship_view(self, view: tuple[str, TerminalView | None] | None, *,
                          consecutive: bool = False) -> None:
        # The reactor strip belongs to the whole ship even when the player
        # changes the panel selection. Other readings retain their own scope.
        self._clear_pending(keep_reactor_confirmation=bool(
            consecutive and self._ship_view and self._ship_view[1] and view and view[1]
        ))
        for track in self._tracks.values():
            track.known_power = None
            track.power_loss_pending = False
        self._ship_view = view

    def _track_key(self, reading_key: str) -> tuple[str | None, str]:
        terminal = self._ship_view[1] if self._ship_view is not None else None
        subject = None if terminal is None or reading_key == _REACTOR_KEY else terminal.subject
        if self._ship_view and self._ship_view[0] == "ground_vehicle":
            subject = "\0ground_vehicle"  # Distinct from a printable terminal subject.
        return (subject, reading_key)

    def _remember_capture(self, capture_id: str, captured_ms: int, now_ms: int) -> None:
        self._last_capture_ms = captured_ms
        self._last_capture_observed_ms = now_ms
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
        personal_context = scene == "fps" and (
            "suit_oxygen" in self.category_thresholds
            or bool(self.personal_status_categories)
        )
        if (scene not in VEHICLE_SCENES and not personal_context) or (
            scene == "engineering_terminal" and terminal is None
        ):
            # An older or duplicate frame can withdraw offers, but cannot
            # replace a newer scene's power baseline.
            if (capture_id not in self._seen and (self._last_capture_ms is None
                    or captured_ms > self._last_capture_ms)):
                self._change_ship_view(None)
                self._remember_capture(capture_id, captured_ms, now_ms)
            else:
                self._clear_pending()
            return ()
        if capture_id in self._seen:
            return ()
        if self._last_capture_ms is not None and captured_ms <= self._last_capture_ms:
            self._clear_pending()
            return ()

        view = (scene, terminal)
        # Measure idle time after the prior result. Its own inference time was
        # already checked separately and should not count as a capture outage.
        capture_gap = (self._last_capture_observed_ms is not None
                       and captured_ms - self._last_capture_observed_ms > self.freshness_ms)
        if view != self._ship_view or capture_gap:
            self._change_ship_view(view, consecutive=not capture_gap)

        self._offers.clear()
        self._checked = ()
        self._remember_capture(capture_id, captured_ms, now_ms)

        by_key: dict[str, Reading] = {}
        for row in readings:
            if not scene_admits_kind(scene, row.kind):
                continue
            if (row.kind in _PERSONAL_READINGS
                    and self._category(row.kind) not in self.personal_status_categories):
                continue
            if row.kind in _HUD_WARNING_KINDS and scene not in {"cockpit", "ground_vehicle"}:
                continue
            if row.kind == "reactor_explosion_imminent_warning" and scene != "cockpit" and terminal is None:
                continue
            if terminal is not None and not terminal.admits(row.kind, row):
                continue
            key = self._key(row)
            if key in by_key:
                raise ValueError("duplicate reading")
            by_key[key] = row
        self._visible = frozenset(self._category(row.kind) for row in by_key.values())
        low_threshold, _ = self.levels("shields")
        shield_rows = (row for row in by_key.values() if row.kind == "shield_charge")
        by_face: dict[str, set[float]] = {}
        for row in shield_rows:
            faces = _shield_faces(row.label)
            if len(faces) == 1:
                by_face.setdefault(next(iter(faces)), set()).add(float(row.value))
        self._visible_low_shield_faces = frozenset(
            face for face, values in by_face.items()
            if len(values) == 1 and next(iter(values)) <= low_threshold
        )
        if (self._last_spoken_shield is not None
                and self._last_spoken_shield[0] not in self._visible_low_shield_faces):
            self._last_spoken_shield = None

        visible_tracks = {self._track_key(key) for key in by_key}
        for track_key, track in self._tracks.items():
            if track_key not in visible_tracks:
                track.candidate, track.captures = None, set()
                # Missing is unknown: a later OFF cannot prove a transition
                # from an ON state that was no longer observed.
                track.known_power = None
                track.power_loss_pending = False

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

            if row.kind in _PERSONAL_STATUS:
                target = row.value
                candidate = ("personal_status", target)
            elif row.kind in _PERCENT:
                target = self._severity(track, _number(row.value), self._category(row.kind))
                candidate = ("percent", target)
            else:
                target = int(row.value)
                if row.kind == "power_online" and target:
                    track.power_loss_pending = False
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
                track.power_loss_pending = was_on or track.power_loss_pending
                if not track.power_loss_pending:
                    continue
                target = 2
            elif row.kind in _PERSONAL_STATUS:
                if target == "recovered":
                    track.active = 0
                    continue
                target = 2 if target == "dangerously_low" else 1
            elif row.kind in _BOOLEAN:
                if target == 0:
                    track.active = 0
                    continue
                target = 2 if row.kind in {
                    "component_failed", "emp_warning", "fire_warning",
                    "reactor_explosion_imminent_warning", "shields_critical_warning",
                } else 1

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
            reactor_repeat_due = (
                row.kind == "reactor_explosion_imminent_warning"
                and self._reactor_last_attempt_ms is not None
                and now_ms - self._reactor_last_attempt_ms >= self.repeat_cooldown_ms
            )
            if (row.kind == "reactor_explosion_imminent_warning"
                    and self._reactor_last_attempt_ms is not None and not reactor_repeat_due):
                continue
            noted = self._external.get(self._category(row.kind))
            if (severity == "warning" and noted is not None
                    and 0 <= now_ms - noted < self.repeat_cooldown_ms):
                self._spent_externally(key, noted)
                continue
            escalation_unspoken = (
                severity == "critical" and track.last_attempt_severity == "warning"
            )
            if (track.power_loss_pending or transition or track.last_attempt_ms is None
                    or repeat_due or escalation_unspoken or reactor_repeat_due):
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
        if any(item.severity == "critical" and item.key.startswith("shield_charge:") for item in live):
            live = [item for item in live if item.key != _SHIELDS_CRITICAL_KEY]
        # A visible reactor countdown can expire before another frame is read.
        live.sort(key=lambda item: (item.severity != "critical", item.key != _REACTOR_KEY, item.key))
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
                track.power_loss_pending = False
            if (notice.key.startswith("shield_charge:") and notice.severity == "critical"
                    and _SHIELDS_CRITICAL_KEY in self._offers):
                banner_track = self._tracks[self._track_key(_SHIELDS_CRITICAL_KEY)]
                banner_track.last_attempt_ms = self._last_now_ms
                banner_track.last_attempt_severity = "critical"
            if notice.key.startswith("reactor_explosion_imminent_warning:"):
                self._reactor_last_attempt_ms = self._last_now_ms
        self._checked = ()


    def record_warning_delivery(
        self, burst: tuple[Notice, ...], *, spoken: bool, now_ms: int,
    ) -> None:
        """Retain a single clearly spoken target without granting action authority."""
        self._last_spoken_shield = None
        if (not spoken or len(burst) != 1
                or self._ship_view is None or self._ship_view[0] not in SHIP_SCENES):
            return
        notice = burst[0]
        if (notice.category != "shields" or now_ms >= notice.expires_ms
                or self._offers.get(notice.key) is not notice):
            return
        faces = _shield_faces(notice.key.split(":", 1)[1])
        if len(faces) == 1 and faces <= self._visible_low_shield_faces:
            self._last_spoken_shield = (next(iter(faces)), now_ms)

    def last_alerted_shield(self, now_ms: int) -> str | None:
        target = self._last_spoken_shield
        if (target is None or not 0 <= now_ms - target[1] < _WARNING_REFERENCE_MS
                or self._last_capture_ms is None
                or not 0 <= now_ms - self._last_capture_ms < self.freshness_ms
                or "shields" in self.muted_categories | self.voice_muted
                or target[0] not in self._visible_low_shield_faces):
            return None
        return target[0]

    def clear_warning_reference(self) -> None:
        self._last_spoken_shield = None

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
        if category == "shields":
            self.clear_warning_reference()
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
        if category == "shields":
            self.clear_warning_reference()
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
