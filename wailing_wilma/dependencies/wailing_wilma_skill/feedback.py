"""Local intake of player reports about submitted Wilma warnings."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

FEEDBACK_DATABASE = "warning-feedback.sqlite3"
FEEDBACK_ISSUES = frozenset({"wrong", "late", "repeated", "missed"})


@dataclass(frozen=True, slots=True)
class FeedbackCapture:
    capture_id: str
    captured_at_utc: str
    capture_source: str
    scene: str
    context: str | None


@dataclass(frozen=True, slots=True)
class FeedbackNotice:
    category: str
    severity: str
    text: str


@dataclass(frozen=True, slots=True)
class WarningSubmission:
    warning_id: str
    capture: FeedbackCapture
    image_encoding_sha256: str
    capture_to_submit_ms: int
    reader_provider: str
    configured_model: str
    notices: tuple[FeedbackNotice, ...]


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _feedback_table(database: sqlite3.Connection) -> None:
    database.execute("""CREATE TABLE IF NOT EXISTS warning_feedback (
        warning_id TEXT PRIMARY KEY NOT NULL,
        reported_at_utc TEXT NOT NULL, issue TEXT NOT NULL, note TEXT,
        submission_json TEXT NOT NULL)""")


def record_feedback(directory: str, warning: WarningSubmission, issue: str,
                    note: str | None) -> bool:
    path = Path(directory) / FEEDBACK_DATABASE
    with closing(sqlite3.connect(path)) as database, database:
        _feedback_table(database)
        created = database.execute(
            "INSERT INTO warning_feedback VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(warning_id) DO NOTHING",
            (warning.warning_id, utc_timestamp(), issue, note, json.dumps(asdict(warning))),
        )
        return created.rowcount == 1


def record_missed_warning(directory: str, note: str, reported_at: str, *,
                          monitor_running: bool, muted: bool, screen_source: str) -> None:
    """Record what the player reported and the watch status at report time."""
    with closing(sqlite3.connect(Path(directory) / FEEDBACK_DATABASE)) as database, database:
        _feedback_table(database)  # Earlier export tools can still read their original reports.
        database.execute("""CREATE TABLE IF NOT EXISTS missed_warnings (
            reported_at_utc TEXT NOT NULL, note TEXT NOT NULL, monitor_running INTEGER NOT NULL,
            muted INTEGER NOT NULL, screen_source TEXT NOT NULL)""")
        database.execute("INSERT INTO missed_warnings VALUES (?, ?, ?, ?, ?)",
                         (reported_at, note, monitor_running, muted, screen_source))


def export_feedback(path: Path) -> dict:
    """Read a review queue. These records have no independent ground-truth label."""
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        database.execute("BEGIN")  # Read both collections from one snapshot.
        rows = database.execute("""SELECT reported_at_utc, issue, note, submission_json
            FROM warning_feedback ORDER BY reported_at_utc, warning_id""")
        reports = [{"reported_at_utc": at, "issue": issue, "player_note": note,
                    "submission": json.loads(submission)} for at, issue, note, submission in rows]
        result = {"schema_version": 1, "review_status": "unreviewed_player_reports",
                  "reports": reports}
        has_misses = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='missed_warnings'"
        ).fetchone()
        if has_misses:
            rows = database.execute("""
                SELECT reported_at_utc, note, monitor_running, muted, screen_source
                FROM missed_warnings ORDER BY reported_at_utc, rowid""")
            result["missed_warnings"] = [
                {"reported_at_utc": at, "player_note": note, "at_report_time": {
                    "monitor_running": bool(running), "muted": bool(muted), "screen_source": source,
                }}
                for at, note, running, muted, source in rows
            ]
    return result
