"""Translate visible engineering console evidence into a Wilma visual report."""

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.perception.wilma_terminal_ocr import ReactorReading

from integrations.wingman_wilma.policy import (
    _JSON_FENCE,
    _MAX_TEXT,
    _SHIP_OVERVIEW_CUES,
    _STATUS_LABELS,
    _constant,
    _failure_strip_subject,
    _has_ship_status_labels,
    _pairs,
    _terminal_view,
    parse_visual_report,
)

TRANSCRIPTION_PROMPT = (
    "Transcribe the visible Star Citizen engineering display. Return exactly this flat "
    'JSON: {"active_tab":null,"filters":[],"status_labels":[],"banner":null}. '
    "Fill active_tab with the "
    "visible selected ship map tab title. Fill filters with the exact labels in the map "
    "filter legend. Copy the readable gauge headings in the status row immediately above "
    "the warning into status_labels. Copy the entire active red warning line on that same "
    "display into banner, including the component name, qualifiers or countdown. "
    "Leave each missing or unreadable field "
    "empty. Ordinary flame effects, mission objectives, component popovers, buttons, "
    "notification history, and suit HUD are not this banner."
)
_REACTOR_LINE = re.compile(
    r"(REACTOR\s+CRITICAL\s*:?\s+EXPLOSION\s+IMMINENT)(?:\s+([0-9]{1,3})\s+SECONDS?)?[.!]?",
    re.IGNORECASE,
)


def reactor_overview_reply(transcription: str) -> str:
    """Interpret a literal banner only in an identifiable same-frame panel."""
    unknown = '{"scene":"unknown","readings":[]}'
    if not isinstance(transcription, str) or len(transcription) > _MAX_TEXT:
        return unknown
    fenced = _JSON_FENCE.fullmatch(transcription)
    if fenced is not None:
        transcription = fenced.group("body")
    try:
        value = json.loads(transcription, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError):
        return unknown
    required = {"active_tab", "filters", "banner"}
    if not isinstance(value, dict) or set(value) not in (required, required | {"status_labels"}):
        return unknown
    tab, filters, banner = value["active_tab"], value["filters"], value["banner"]
    if (tab is not None and not isinstance(tab, str)
            or not isinstance(filters, list) or not all(isinstance(word, str) for word in filters)):
        return unknown
    component = _failure_strip_subject(banner)
    cues = [word.strip() for word in filters if word.strip().casefold() in _SHIP_OVERVIEW_CUES]
    terminal = {"page": tab, "overview_cues": cues}
    if _terminal_view(terminal) is None:
        if not component or not _has_ship_status_labels(value.get("status_labels")):
            return unknown
        terminal = {"status_labels": value["status_labels"], "active_banner": banner}
    panel = {"scene": "engineering_terminal", "terminal": terminal, "readings": []}
    if not isinstance(banner, str):
        return json.dumps(panel)
    kind = "component_failed" if component else "reactor_explosion_imminent_warning"
    panel["readings"] = [{"kind": kind,
                          "label": component.capitalize() if component else "Reactor",
                          "value": True, "evidence": banner}]
    try:
        valid = parse_visual_report(json.dumps(panel)).readings
    except ValueError:
        valid = ()
    if not valid:
        panel["readings"] = []
    return json.dumps(panel)


def native_reactor_reply(reading: "ReactorReading") -> str:
    """Pass literal, same-panel reactor or component-failure evidence to policy."""
    unknown = '{"scene":"unknown","readings":[]}'
    if not reading.console_identified or not reading.text:
        return unknown
    component = _failure_strip_subject(reading.text)
    if component is not None:
        part = component.capitalize()
        banner = reading.text.strip().upper()
        status_labels = sorted({
            cue.partition(":")[2] for cue in reading.console_cues
            if cue.casefold().startswith("status:")
            and cue.partition(":")[2].casefold() in _STATUS_LABELS
        })
        if len(status_labels) < 2:
            return unknown
        report = {
            "scene": "engineering_terminal",
            "terminal": {"status_labels": status_labels, "active_banner": banner},
            "readings": [{"kind": "component_failed", "label": part,
                          "value": True, "evidence": banner}],
        }
        reply = json.dumps(report)
        try:
            return reply if parse_visual_report(reply).readings else unknown
        except ValueError:
            return unknown
    line = _REACTOR_LINE.fullmatch(reading.text.strip())
    if line is None:
        return unknown
    evidence = (line.group(0) if line.group(2) is not None and int(line.group(2)) == reading.seconds
                else line.group(1))
    cues = {cue.casefold() for cue in reading.console_cues}
    selected = next((label for label in sorted(_SHIP_OVERVIEW_CUES)
                     if f"active_filter:{label}" in cues
                     and f"filter_option:{label}" in cues), None)
    if not selected or "filter_menu:filters" not in cues:
        return unknown
    report = {
        "scene": "engineering_terminal",
        "terminal": {
            "status_labels": sorted(label for label in _STATUS_LABELS if f"status:{label}" in cues),
            "filter_heading": "FILTERS",
            "highlighted_filter": selected,
        },
        "readings": [{"kind": "reactor_explosion_imminent_warning", "label": "Reactor",
                      "value": True, "evidence": evidence}],
    }
    reply = json.dumps(report)
    return reply if parse_visual_report(reply).readings else unknown
