"""Shared helpers: paths, settings, timestamps, database connection, checksums."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "sql" / "schema.sql"
SETTINGS_PATH = ROOT / "config" / "settings.json"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Parent tables first so CSVs can be loaded with foreign keys switched on.
TABLE_ORDER = [
    "learner",
    "assessment",
    "integrity_rule",
    "reviewer",
    "reviewer_conflict",
    "submission",
    "similarity_report",
    "integrity_case",
    "case_reviewer",
    "evidence_item",
    "decision",
    "appeal",
    "case_event",
]

PRIMARY_KEYS = {
    "learner": "learner_id",
    "assessment": "assessment_id",
    "integrity_rule": "integrity_rule_id",
    "reviewer": "reviewer_id",
    "reviewer_conflict": "conflict_id",
    "submission": "submission_id",
    "similarity_report": "similarity_report_id",
    "integrity_case": "case_id",
    "case_reviewer": "case_reviewer_id",
    "evidence_item": "evidence_id",
    "decision": "decision_id",
    "appeal": "appeal_id",
    "case_event": "event_id",
}


def load_settings(path: Path | None = None) -> dict:
    with open(path or SETTINGS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
