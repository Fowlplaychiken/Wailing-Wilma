"""Saved warning controls supplied by Wingman's normal skill configuration."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .policy import DEFAULT_CONFIRMATIONS, MAX_CONFIRMATIONS, WilmaPolicy
from .reader import ReaderSettings

CONFIRMATIONS_PROPERTY = "warning_confirmation_frames"
PERCENT_CATEGORIES = {
    "shields": "Shield charge", "components": "Component health",
    "hull": "Hull integrity", "armor": "Armor remaining", "fuel": "Fuel",
    "quantum_fuel": "Quantum fuel", "life_support": "Oxygen",
}
OTHER_CATEGORIES = {"power": "Power loss", "heat": "Overheating", "fire": "Fire",
                    "environment": "Environment", "emp": "Explicit EMP",
                    "hydration": "Hydration", "nutrition": "Nutrition",
                    "blood_loss": "Blood loss", "pressure": "High pressure",
                    "incapacitation": "Incapacitation"}


@dataclass(frozen=True)
class Preferences:
    interval_seconds: float
    freshness_ms: int
    cooldown_ms: int
    category_thresholds: dict[str, tuple[float, float]]
    muted_categories: frozenset[str]
    personal_status_categories: frozenset[str] = frozenset()
    confirmations: int = DEFAULT_CONFIRMATIONS
    image_detail: str = "stock"
    reader: ReaderSettings = ReaderSettings()
    reader_secret: str = ""

    def warning_policy(self) -> WilmaPolicy:
        return WilmaPolicy(
            confirmations=self.confirmations,
            freshness_ms=self.freshness_ms,
            repeat_cooldown_ms=self.cooldown_ms,
            category_thresholds=self.category_thresholds,
            personal_status_categories=self.personal_status_categories,
            muted_categories=self.muted_categories,
        )


def read_reader_settings(get: Callable[[str], object]) -> tuple[ReaderSettings, str]:
    """Read the selected route only; saved inactive fields do not block stock use."""
    provider = get("vision_reader")
    provider = "wingman" if provider is None else provider
    if provider == "wingman":
        return ReaderSettings(), ""
    try:
        if type(provider) is not str or provider not in {"openrouter", "local", "ollama", "waterfall"}:
            raise ValueError
        model = get("vision_reader_model")
        model = "" if model is None else model
        endpoint = get("vision_reader_endpoint") if provider != "openrouter" else ""
        endpoint = "" if endpoint is None else endpoint
        structured = get("vision_reader_json")
        structured = False if structured is None else structured
        no_reasoning = get("vision_reader_no_reasoning")
        no_reasoning = False if no_reasoning is None else no_reasoning
        secret = "openrouter" if provider == "openrouter" else get("vision_reader_secret")
        secret = "" if secret is None else secret
        if (type(secret) is not str or len(secret) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in secret)):
            raise ValueError
        if provider == "waterfall" and not secret.strip():
            raise ValueError
        return ReaderSettings(provider, model, endpoint, structured, no_reasoning), secret
    except (ValueError, TypeError):
        raise ValueError("Invalid Wailing Wilma setting: vision reader") from None


def read_preferences(get: Callable[[str], object]) -> Preferences:
    def number(name: str, default: float, *, minimum: float = 0,
               maximum: float | None = None, positive: bool = False) -> float:
        value = get(name)
        value = default if value is None else value
        try:
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and value >= minimum and not (positive and value <= 0)
                     and (maximum is None or value <= maximum))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"Invalid Wailing Wilma setting: {name}")
        return float(value)

    def milliseconds(name: str, default: float, *, positive: bool = False) -> int:
        value = number(name, default, positive=positive) * 1000
        if not math.isfinite(value) or value > 2**63 - 1:
            raise ValueError(f"Invalid Wailing Wilma setting: {name}")
        return max(1 if positive else 0, int(value))

    critical = number("critical_percent", 5, maximum=100)
    thresholds, muted, personal_status = {}, set(), set()
    for category in PERCENT_CATEGORIES:
        low = number(f"{category}_low_percent", 20, minimum=critical, maximum=100)
        thresholds[category] = (low, critical)
    for category in PERCENT_CATEGORIES | OTHER_CATEGORIES:
        enabled = get(f"warn_{category}")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError(f"Invalid Wailing Wilma setting: warn_{category}")
        if category in {"hydration", "nutrition", "blood_loss", "pressure", "incapacitation"}:
            if enabled is True:
                personal_status.add(category)
            # Keep this opt-in outside the legacy muted set. A missing property
            # in an older profile must preserve its saved-policy identity.
        elif enabled is False:
            muted.add(category)
    suit_oxygen = get("warn_suit_oxygen")
    if suit_oxygen is not None and type(suit_oxygen) is not bool:
        raise ValueError("Invalid Wailing Wilma setting: warn_suit_oxygen")
    if suit_oxygen is True:
        # The threshold entry is also the policy's explicit personal-HUD opt-in.
        # Older/disabled profiles retain their existing policy identity.
        thresholds["suit_oxygen"] = (
            number("suit_oxygen_low_percent", 20, minimum=critical, maximum=100), critical)
    high_detail = get("high_detail_images")
    if high_detail is not None and type(high_detail) is not bool:
        raise ValueError("Invalid Wailing Wilma setting: high_detail_images")
    reader, secret = read_reader_settings(get)
    confirmations = number(CONFIRMATIONS_PROPERTY, DEFAULT_CONFIRMATIONS,
                           minimum=1, maximum=MAX_CONFIRMATIONS)
    if not confirmations.is_integer():
        raise ValueError(f"Invalid Wailing Wilma setting: {CONFIRMATIONS_PROPERTY}")
    return Preferences(
        interval_seconds=number("sample_interval_seconds", 1, positive=True),
        freshness_ms=milliseconds("maximum_reading_age_seconds", 3, positive=True),
        cooldown_ms=milliseconds("repeat_cooldown_seconds", 60),
        category_thresholds=thresholds, muted_categories=frozenset(muted),
        personal_status_categories=frozenset(personal_status),
        confirmations=int(confirmations),
        image_detail="high" if high_detail is True else "stock",
        reader=reader, reader_secret=secret,
    )
