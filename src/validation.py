"""Validation, reconciliation, metrics, ground-truth evaluation and reproducibility.

Usage:
    python -m src.validation --scale full --run-all

Writes outputs/validation_summary_<scale>.json (machine-readable) and
outputs/validation_report_<scale>.md (human-readable). Exit code 1 if any check fails.

Ground truth is read ONLY in the evaluation step, after the operational checks have run.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from . import rules
from .common import DATA_DIR, OUTPUT_DIR, PRIMARY_KEYS, TABLE_ORDER, connect, load_settings, parse_iso
from .database import db_path_for, init_db

TS_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"

ID_PATTERNS = {
    "learner": r"LRN-2026-\d{6}",
    "assessment": r"ASM-2026-\d{4}",
    "integrity_rule": r"INT-2026-\d{6}",
    "reviewer": r"REV-2026-\d{4}",
    "reviewer_conflict": r"CONF-2026-\d{6}",
    "submission": r"SUB-2026-\d{6}",
    "similarity_report": r"SIM-SUB-2026-\d{6}-\d{2}",
    "integrity_case": r"CASE-2026-\d{6}",
    "case_reviewer": r"ASG-CASE-2026-\d{6}-\d{2}",
    "evidence_item": r"EVD-CASE-2026-\d{6}-\d{3}",
    "decision": r"DEC-CASE-2026-\d{6}-\d{2}",
    "appeal": r"APL-CASE-2026-\d{6}-\d{2}",
    "case_event": r"EVT-CASE-2026-\d{6}-\d{4}",
}

ENUMS = [
    ("assessment", "assessment_type", rules.ASSESSMENT_TYPES),
    ("reviewer", "reviewer_role", rules.REVIEWER_ROLES),
    ("similarity_report", "score_band", rules.SCORE_BANDS),
    ("integrity_case", "review_priority", ("low", "standard", "priority")),
    ("integrity_case", "status", rules.CASE_STATUSES),
    ("case_reviewer", "assignment_role", rules.ASSIGNMENT_ROLES),
    ("case_reviewer", "assignment_status", rules.ASSIGNMENT_STATUSES),
    ("evidence_item", "evidence_type", rules.EVIDENCE_TYPES),
    ("decision", "decision_source", rules.DECISION_SOURCES),
    ("decision", "outcome", rules.REVIEW_OUTCOMES),
    ("appeal", "outcome", rules.APPEAL_OUTCOMES),
    ("case_event", "event_type", rules.EVENT_TYPES),
]

TIMESTAMP_COLUMNS = [
    ("assessment", "due_at"), ("integrity_rule", "effective_from"), ("integrity_rule", "effective_to"),
    ("reviewer_conflict", "declared_at"), ("submission", "submitted_at"), ("similarity_report", "generated_at"),
    ("integrity_case", "opened_at"), ("integrity_case", "closed_at"), ("case_reviewer", "assigned_at"),
    ("case_reviewer", "unassigned_at"), ("evidence_item", "added_at"), ("decision", "decided_at"),
    ("appeal", "submitted_at"), ("appeal", "reviewer_assigned_at"), ("appeal", "decided_at"),
    ("case_event", "event_at"),
]

DATE_ORDER_SQL = {
    "report_after_submission": """
        SELECT r.similarity_report_id FROM similarity_report r JOIN submission s USING (submission_id)
        WHERE r.generated_at < s.submitted_at""",
    "case_opened_after_report": """
        SELECT c.case_id FROM integrity_case c JOIN similarity_report r USING (similarity_report_id)
        WHERE c.opened_at < r.generated_at""",
    "assignment_after_case_open": """
        SELECT cr.case_reviewer_id FROM case_reviewer cr JOIN integrity_case c USING (case_id)
        WHERE cr.assigned_at < c.opened_at""",
    "evidence_after_case_open": """
        SELECT e.evidence_id FROM evidence_item e JOIN integrity_case c USING (case_id) WHERE e.added_at < c.opened_at""",
    "decision_after_case_open": """
        SELECT d.decision_id FROM decision d JOIN integrity_case c USING (case_id) WHERE d.decided_at < c.opened_at""",
    "appeal_after_decision": """
        SELECT a.appeal_id FROM appeal a JOIN decision d USING (decision_id) WHERE a.submitted_at <= d.decided_at""",
    "appeal_review_order": """
        SELECT appeal_id FROM appeal WHERE (reviewer_assigned_at IS NOT NULL AND reviewer_assigned_at < submitted_at)
           OR (decided_at IS NOT NULL AND decided_at < submitted_at)""",
    "closure_after_decisions_and_appeals": """
        SELECT c.case_id FROM integrity_case c WHERE c.closed_at IS NOT NULL AND (
            c.closed_at < (SELECT MAX(decided_at) FROM decision d WHERE d.case_id = c.case_id)
            OR c.closed_at < COALESCE((SELECT MAX(decided_at) FROM appeal a WHERE a.case_id = c.case_id), ''))""",
}

DETECTORS = {
    "automatic_guilt": """
        SELECT d.case_id, d.decision_id FROM decision d
        WHERE d.decision_source <> 'human_review' OR d.reviewer_id IS NULL
           OR NOT EXISTS (SELECT 1 FROM case_reviewer cr
                          WHERE cr.case_id = d.case_id AND cr.reviewer_id = d.reviewer_id
                            AND cr.assignment_status IN ('active','completed') AND cr.assigned_at <= d.decided_at)""",
    "reviewer_conflict": """
        SELECT cr.case_id, cr.case_reviewer_id FROM case_reviewer cr
        JOIN integrity_case c ON c.case_id = cr.case_id
        JOIN submission s ON s.submission_id = c.submission_id
        JOIN reviewer_conflict rc ON rc.reviewer_id = cr.reviewer_id AND rc.learner_id = s.learner_id
                                  AND rc.declared_at <= cr.assigned_at
        WHERE cr.assignment_status IN ('active','completed')""",
    "evidence_history_missing": """
        SELECT e.case_id, e.evidence_id FROM evidence_item e
        WHERE NOT EXISTS (SELECT 1 FROM case_event ev WHERE ev.case_id = e.case_id
                          AND ev.event_type = 'evidence_added' AND ev.entity_id = e.evidence_id)""",
    "appeal_closed_without_independent_review": """
        SELECT a.case_id, a.appeal_id FROM appeal a JOIN decision d ON d.decision_id = a.decision_id
        WHERE a.outcome <> 'pending' AND (
            a.appeal_reviewer_id IS NULL
            OR a.appeal_reviewer_id = d.reviewer_id
            OR EXISTS (SELECT 1 FROM case_reviewer cr WHERE cr.case_id = a.case_id
                       AND cr.reviewer_id = a.appeal_reviewer_id AND cr.assignment_role IN ('primary','secondary')
                       AND cr.assignment_status <> 'blocked_conflict')
            OR NOT EXISTS (SELECT 1 FROM case_event ev WHERE ev.case_id = a.case_id
                           AND ev.event_type = 'appeal_reviewer_assigned' AND ev.entity_id = a.appeal_id))""",
}


def _rows(conn, sql, params=()):
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def _check(name, category, description, violations, total=None):
    violations = list(violations)
    return {"name": name, "category": category, "description": description, "passed": not violations,
            "violations": len(violations), "checked": total, "sample": [list(v) if isinstance(v, tuple) else v
                                                                         for v in violations[:5]]}


# --------------------------------------------------------------- operational checks
def structural_checks(conn, settings, params) -> list[dict]:
    out = []
    expected = {"submission": params["submissions"], "similarity_report": params["similarity_reports"],
                "integrity_case": params["cases"]}
    for table, n in expected.items():
        actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out.append(_check(f"row_count_{table}", "structure", f"{table} has the configured {n} rows",
                          [] if actual == n else [(table, actual, n)], 1))
    for table in TABLE_ORDER:
        pk = PRIMARY_KEYS[table]
        dup = conn.execute(f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {table}").fetchone()[0]
        nulls = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {pk} IS NULL").fetchone()[0]
        out.append(_check(f"pk_unique_{table}", "structure", f"{pk} is unique and non-null",
                          [(table, dup, nulls)] if dup or nulls else []))
        pattern = re.compile(ID_PATTERNS[table] + r"\Z")
        bad = [r[0] for r in conn.execute(f"SELECT {pk} FROM {table}") if not pattern.match(r[0])]
        out.append(_check(f"id_format_{table}", "structure", f"{pk} matches {ID_PATTERNS[table]}", bad))
    fk = _rows(conn, "PRAGMA foreign_key_check")
    out.append(_check("foreign_keys_all_tables", "structure", "PRAGMA foreign_key_check finds no orphan rows", fk))
    id_links = {
        "report_id_embeds_submission": "SELECT similarity_report_id FROM similarity_report "
                                       "WHERE similarity_report_id <> 'SIM-' || submission_id || '-01'",
        "case_matches_report_submission": "SELECT c.case_id FROM integrity_case c JOIN similarity_report r "
                                          "USING (similarity_report_id) WHERE c.submission_id <> r.submission_id",
    }
    for table, prefix in [("evidence_item", "EVD"), ("decision", "DEC"), ("appeal", "APL"),
                          ("case_event", "EVT"), ("case_reviewer", "ASG")]:
        pk = PRIMARY_KEYS[table]
        id_links[f"{table}_id_embeds_case"] = f"SELECT {pk} FROM {table} WHERE {pk} NOT LIKE '{prefix}-' || case_id || '-%'"
    for name, sql in id_links.items():
        out.append(_check(name, "structure", "IDs are reused consistently across tables", _rows(conn, sql)))
    for table, col, allowed in ENUMS:
        bad = [r for r in _rows(conn, f"SELECT {PRIMARY_KEYS[table]}, {col} FROM {table}") if r[1] not in allowed]
        out.append(_check(f"enum_{table}_{col}", "structure", f"{col} uses documented values", bad))
    for table, col in TIMESTAMP_COLUMNS:
        bad = _rows(conn, f"SELECT {PRIMARY_KEYS[table]}, {col} FROM {table} "
                          f"WHERE {col} IS NOT NULL AND {col} NOT GLOB '{TS_GLOB}'")
        out.append(_check(f"timestamp_{table}_{col}", "structure", "ISO 8601 UTC format", bad))
        if table != "integrity_rule":
            future = _rows(conn, f"SELECT {PRIMARY_KEYS[table]}, {col} FROM {table} WHERE {col} > ?",
                           (settings["as_of"],))
            out.append(_check(f"not_after_as_of_{table}_{col}", "structure",
                              f"no {col} after the snapshot date {settings['as_of']}", future))
    ranges = _rows(conn, "SELECT similarity_report_id FROM similarity_report WHERE similarity_score NOT BETWEEN 0 AND 100 "
                         "OR quoted_material_share NOT BETWEEN 0 AND 1 OR matched_source_count < 0")
    out.append(_check("value_ranges", "structure", "score 0-100, quoted share 0-1, counts >= 0", ranges))
    dups = _rows(conn, "SELECT learner_id, assessment_id, COUNT(*) FROM submission "
                       "GROUP BY learner_id, assessment_id HAVING COUNT(*) > 1")
    out.append(_check("duplicate_submissions", "structure", "one submission per learner per assessment", dups))

    rule_rows = [dict(r) for r in conn.execute("SELECT * FROM integrity_rule")]
    bad_band = []
    for r in conn.execute("SELECT * FROM similarity_report"):
        rule = rules.active_rule(rule_rows, r["generated_at"])
        band = rules.score_band(r["similarity_score"], rule)
        if rule["integrity_rule_id"] != r["integrity_rule_id"] or band != r["score_band"] \
                or r["is_flagged"] != int(band != "none"):
            bad_band.append(r["similarity_report_id"])
    out.append(_check("score_band_matches_active_rule", "structure",
                      "band and flag recomputed from the rule in force at report time", bad_band))
    case_flag = _rows(conn, "SELECT c.case_id FROM integrity_case c JOIN similarity_report r USING (similarity_report_id) "
                            "WHERE r.is_flagged = 0 OR c.review_priority <> r.score_band "
                            "OR c.integrity_rule_id <> r.integrity_rule_id")
    out.append(_check("cases_only_from_flags", "structure", "every case comes from a flagged report", case_flag))

    bad_seq, bad_status = [], []
    for case in conn.execute("SELECT case_id, status, closed_at FROM integrity_case"):
        events = _rows(conn, "SELECT event_seq, event_at, event_type FROM case_event WHERE case_id=? ORDER BY event_seq",
                       (case["case_id"],))
        if [e[0] for e in events] != list(range(1, len(events) + 1)) or \
                any(events[i][1] > events[i + 1][1] for i in range(len(events) - 1)):
            bad_seq.append(case["case_id"])
        derived = rules.derive_status([e[2] for e in events])
        if derived != case["status"] or (case["status"] == "closed") != (case["closed_at"] is not None):
            bad_status.append((case["case_id"], case["status"], derived))
    out.append(_check("event_sequence_ordered", "structure", "event_seq is 1..n and time never goes backwards", bad_seq))
    out.append(_check("status_derived_from_events", "structure",
                      "stored case status equals the status replayed from the event history", bad_status))

    actors = _rows(conn, """
        SELECT e.evidence_id, e.added_by FROM evidence_item e JOIN integrity_case c USING (case_id)
        JOIN submission s ON s.submission_id = c.submission_id
        WHERE e.added_by NOT LIKE 'SYSTEM-%' AND e.added_by <> s.learner_id
          AND e.added_by NOT IN (SELECT reviewer_id FROM reviewer)""")
    out.append(_check("evidence_actor_valid", "structure", "added_by is a reviewer, the learner or a system", actors))

    for name, sql in DATE_ORDER_SQL.items():
        out.append(_check(name, "date_order", "timestamps follow the lifecycle order", _rows(conn, sql)))
    return out


def workflow_checks(conn, settings) -> list[dict]:
    window = timedelta(days=settings["appeal_window_days"])
    min_len = settings["min_rationale_chars"]
    out = []
    latest = {r["case_id"]: dict(r) for r in conn.execute(
        "SELECT d.* FROM decision d WHERE decision_round = (SELECT MAX(decision_round) FROM decision x "
        "WHERE x.case_id = d.case_id)")}
    closed = [dict(r) for r in conn.execute("SELECT * FROM integrity_case WHERE status = 'closed'")]
    appeals = [dict(r) for r in conn.execute("SELECT * FROM appeal")]
    appealed_decisions = {a["decision_id"] for a in appeals}

    bad = [c["case_id"] for c in closed if c["case_id"] not in latest
           or latest[c["case_id"]]["decision_source"] != "human_review"
           or len(latest[c["case_id"]]["rationale"].strip()) < min_len]
    out.append(_check("closure_requires_human_decision", "workflow",
                      "no case closes without a human decision and rationale", bad, len(closed)))
    bad = [c["case_id"] for c in closed
           if c["case_id"] in latest and latest[c["case_id"]]["outcome"] in rules.ADVERSE_OUTCOMES
           and latest[c["case_id"]]["decision_id"] not in appealed_decisions
           and latest[c["case_id"]]["supersedes_decision_id"] is None
           and parse_iso(c["closed_at"]) <= parse_iso(latest[c["case_id"]]["decided_at"]) + window]
    out.append(_check("adverse_closure_waits_for_appeal_window", "workflow",
                      "adverse decisions stay open for appeal until the window passes", bad))
    out.append(_check("closed_with_pending_appeal", "workflow", "no closed case has a pending appeal",
                      _rows(conn, "SELECT a.appeal_id FROM appeal a JOIN integrity_case c USING (case_id) "
                                  "WHERE c.status='closed' AND a.outcome='pending'")))
    decisions = {r["decision_id"]: dict(r) for r in conn.execute("SELECT * FROM decision")}
    bad = [a["appeal_id"] for a in appeals
           if not (parse_iso(decisions[a["decision_id"]]["decided_at"]) < parse_iso(a["submitted_at"])
                   <= parse_iso(decisions[a["decision_id"]]["decided_at"]) + window)
           or decisions[a["decision_id"]]["outcome"] not in rules.ADVERSE_OUTCOMES]
    out.append(_check("appeal_within_window_and_adverse", "workflow",
                      "appeals only against adverse decisions, inside the window", bad, len(appeals)))
    out.append(_check("human_decision_has_rationale", "workflow", f"rationale >= {min_len} characters",
                      _rows(conn, "SELECT decision_id FROM decision WHERE decision_source='human_review' "
                                  "AND LENGTH(TRIM(rationale)) < ?", (min_len,))))
    out.append(_check("human_decision_after_evidence", "workflow", "reviewer had evidence before deciding",
                      _rows(conn, "SELECT d.decision_id FROM decision d WHERE d.decision_source='human_review' AND "
                                  "NOT EXISTS (SELECT 1 FROM evidence_item e WHERE e.case_id=d.case_id "
                                  "AND e.added_at <= d.decided_at)")))
    out.append(_check("successful_appeal_has_revised_decision", "workflow",
                      "overturned/modified appeals keep the original and add a superseding decision",
                      _rows(conn, "SELECT a.appeal_id FROM appeal a WHERE a.outcome IN ('overturned','modified') "
                                  "AND NOT EXISTS (SELECT 1 FROM decision d WHERE d.supersedes_decision_id = a.decision_id)")))
    return out


def detect_anomalies(conn) -> dict[str, list[tuple]]:
    return {name: _rows(conn, sql) for name, sql in DETECTORS.items()}


# ----------------------------------------------------------- metrics/reconcile
def audit_completeness(conn) -> dict:
    required = {
        "case_opened": "SELECT case_id, case_id FROM integrity_case",
        "reviewer_assigned": "SELECT case_id, case_reviewer_id FROM case_reviewer "
                             "WHERE assignment_role <> 'appeal' AND assignment_status <> 'blocked_conflict'",
        "reviewer_assignment_blocked": "SELECT case_id, case_reviewer_id FROM case_reviewer "
                                       "WHERE assignment_status = 'blocked_conflict'",
        "evidence_added": "SELECT case_id, evidence_id FROM evidence_item",
        "decision_recorded": "SELECT case_id, decision_id FROM decision",
        "appeal_submitted": "SELECT case_id, appeal_id FROM appeal",
        "appeal_reviewer_assigned": "SELECT case_id, appeal_id FROM appeal WHERE appeal_reviewer_id IS NOT NULL",
        "appeal_decided": "SELECT case_id, appeal_id FROM appeal WHERE outcome <> 'pending'",
        "case_closed": "SELECT case_id, case_id FROM integrity_case WHERE status = 'closed'",
    }
    events = {(r[0], r[1], r[2]) for r in _rows(conn, "SELECT case_id, event_type, entity_id FROM case_event")}
    events_by_case = {(r[0], r[1]) for r in _rows(conn, "SELECT case_id, event_type FROM case_event")}
    total = covered = 0
    missing = []
    for event_type, sql in required.items():
        for case_id, entity_id in _rows(conn, sql):
            total += 1
            ok = (case_id, event_type) in events_by_case if event_type in ("case_opened", "case_closed") \
                else (case_id, event_type, entity_id) in events
            covered += ok
            if not ok:
                missing.append([event_type, entity_id])
    return {"required_state_changes": total, "with_event": covered,
            "audit_completeness": round(covered / total, 4) if total else 1.0, "missing": missing}


def compute_metrics(conn, settings, detected) -> dict:
    n_sub = conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0]
    n_flag = conn.execute("SELECT COUNT(*) FROM similarity_report WHERE is_flagged = 1").fetchone()[0]
    n_case = conn.execute("SELECT COUNT(*) FROM integrity_case").fetchone()[0]
    first = _rows(conn, "SELECT c.opened_at, d.decided_at FROM decision d JOIN integrity_case c USING (case_id) "
                        "WHERE d.decision_round = 1 AND d.decision_source = 'human_review'")
    days = sorted((parse_iso(d) - parse_iso(o)).total_seconds() / 86400 for o, d in first)
    eligible = conn.execute("SELECT COUNT(*) FROM decision WHERE decision_round = 1 AND decision_source='human_review' "
                            "AND outcome IN ('educational_warning','confirmed_breach')").fetchone()[0]
    n_appeal = conn.execute("SELECT COUNT(*) FROM appeal").fetchone()[0]
    decided_appeals = conn.execute("SELECT COUNT(*) FROM appeal WHERE outcome <> 'pending'").fetchone()[0]
    successful = conn.execute("SELECT COUNT(*) FROM appeal WHERE outcome IN ('overturned','modified')").fetchone()[0]
    non_independent = len(detected["appeal_closed_without_independent_review"])
    workloads = [r[0] for r in _rows(conn, "SELECT COUNT(cr.case_reviewer_id) FROM reviewer rv LEFT JOIN case_reviewer cr "
                                           "ON cr.reviewer_id = rv.reviewer_id AND cr.assignment_status <> 'blocked_conflict' "
                                           "GROUP BY rv.reviewer_id")]
    status = dict(_rows(conn, "SELECT status, COUNT(*) FROM integrity_case GROUP BY status ORDER BY status"))
    outcomes = dict(_rows(conn, "SELECT outcome, COUNT(*) FROM decision d WHERE decision_round = (SELECT MAX(decision_round) "
                                "FROM decision x WHERE x.case_id = d.case_id) GROUP BY outcome ORDER BY outcome"))
    return {
        "flag_rate": round(n_flag / n_sub, 4),
        "case_creation_rate": round(n_case / n_flag, 4) if n_flag else 0,
        "decision_turnaround_days": {
            "count": len(days), "mean": round(statistics.mean(days), 2) if days else None,
            "median": round(statistics.median(days), 2) if days else None,
            "p90": round(days[int(0.9 * (len(days) - 1))], 2) if days else None},
        "appeal_rate": round(n_appeal / eligible, 4) if eligible else 0,
        "appeal_success_rate": round(successful / decided_appeals, 4) if decided_appeals else 0,
        "independent_review_compliance": round((decided_appeals - non_independent) / decided_appeals, 4)
        if decided_appeals else 1.0,
        "conflict_assignment_violations": len(detected["reviewer_conflict"]),
        "conflict_assignments_blocked_by_guard": conn.execute(
            "SELECT COUNT(*) FROM case_reviewer WHERE assignment_status='blocked_conflict'").fetchone()[0],
        "reviewer_workload": {"min": min(workloads), "max": max(workloads),
                              "mean": round(statistics.mean(workloads), 2)},
        "case_status_counts": status,
        "final_outcome_counts": outcomes,
    }


def reconciliation(conn) -> dict:
    c = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    n = {
        "submissions": c("SELECT COUNT(*) FROM submission"),
        "similarity_reports_flagged": c("SELECT COUNT(*) FROM similarity_report WHERE is_flagged = 1"),
        "flags_not_escalated": c("SELECT COUNT(*) FROM similarity_report r WHERE NOT EXISTS "
                                 "(SELECT 1 FROM integrity_case c WHERE c.similarity_report_id = r.similarity_report_id)"),
        "cases": c("SELECT COUNT(*) FROM integrity_case"),
        "cases_with_decision": c("SELECT COUNT(DISTINCT case_id) FROM decision"),
        "cases_awaiting_decision": c("SELECT COUNT(*) FROM integrity_case WHERE case_id NOT IN (SELECT case_id FROM decision)"),
        "decision_records": c("SELECT COUNT(*) FROM decision"),
        "revised_decisions_from_appeals": c("SELECT COUNT(*) FROM decision WHERE supersedes_decision_id IS NOT NULL"),
        "appeals": c("SELECT COUNT(*) FROM appeal"),
        "appeals_pending": c("SELECT COUNT(*) FROM appeal WHERE outcome = 'pending'"),
        "evidence_items": c("SELECT COUNT(*) FROM evidence_item"),
        "closed_cases": c("SELECT COUNT(*) FROM integrity_case WHERE status = 'closed'"),
    }
    n["balanced"] = {
        "flags = escalated + not escalated": n["similarity_reports_flagged"] == n["cases"] + n["flags_not_escalated"],
        "cases = decided + awaiting decision": n["cases"] == n["cases_with_decision"] + n["cases_awaiting_decision"],
        "decision records = cases decided + revised decisions":
            n["decision_records"] == n["cases_with_decision"] + n["revised_decisions_from_appeals"],
    }
    n["explanation"] = (
        "Counts shrink at each stage by design: only stored similarity reports are flags (10% of submissions); "
        "only the highest-ranked flags open a case; cases opened close to the as-of date are still awaiting a "
        "decision; only adverse decisions can be appealed; a successful appeal adds a superseding decision, so "
        "decision records exceed decided cases.")
    return n


# ------------------------------------------------------------- evaluation
def load_ground_truth(gt_dir: Path) -> tuple[list[dict], list[dict]]:
    with open(gt_dir / "anomalies.csv", newline="", encoding="utf-8") as fh:
        anomalies = list(csv.DictReader(fh))
    with open(gt_dir / "report_truth.csv", newline="", encoding="utf-8") as fh:
        truth = list(csv.DictReader(fh))
    return anomalies, truth


def evaluate(conn, detected, gt_dir: Path) -> dict:
    anomalies, truth = load_ground_truth(gt_dir)
    per_type = {}
    for issue_type, found in detected.items():
        expected = {a["entity_id"] for a in anomalies if a["issue_type"] == issue_type}
        got = {row[1] for row in found}
        per_type[issue_type] = {
            "expected": len(expected), "detected": len(got), "true_positives": len(expected & got),
            "missed": sorted(expected - got), "unexpected": sorted(got - expected),
            "passed": expected == got and len(expected) > 0}
    truth_by_report = {t["similarity_report_id"]: t for t in truth}
    final = _rows(conn, "SELECT c.similarity_report_id, d.outcome FROM integrity_case c JOIN decision d USING (case_id) "
                        "WHERE d.decision_source='human_review' AND d.decision_round = (SELECT MAX(decision_round) "
                        "FROM decision x WHERE x.case_id = c.case_id)")
    reviewed = len(final)
    fp = sum(truth_by_report[r]["flag_is_false_positive"] == "1" for r, _ in final)
    adverse = [(r, o) for r, o in final if o in rules.ADVERSE_OUTCOMES]
    wrongly_adverse = sum(truth_by_report[r]["flag_is_false_positive"] == "1" for r, _ in adverse)
    return {
        "anomaly_detection": per_type,
        "false_positive_rate_of_reviewed_flags": round(fp / reviewed, 4) if reviewed else None,
        "reviewed_cases_with_ground_truth": reviewed,
        "adverse_final_outcomes_on_false_positive_flags": wrongly_adverse,
        "adverse_final_outcomes": len(adverse),
        "note": "A high false-positive share among flags is expected and is why every flag goes to human review.",
    }


# --------------------------------------------------------- reproducibility
def reproducibility(scale: str, seed: int, settings: dict) -> dict:
    from .generate_data import generate  # local import keeps validation usable on its own

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as c:
        m1 = generate(scale, seed, Path(a), settings)
        m2 = generate(scale, seed, Path(b), settings)
        m3 = generate(scale, seed + 1, Path(c), settings)
    committed_path = DATA_DIR / "generated" / scale / "manifest.json"
    committed = json.loads(committed_path.read_text()) if committed_path.exists() else None
    return {
        "seed": seed, "scale": scale,
        "run1_equals_run2": m1["files"] == m2["files"] and m1["row_counts"] == m2["row_counts"],
        "files_compared": len(m1["files"]),
        "different_seed_changes_output": m1["files"] != m3["files"],
        "committed_data_matches_code": (committed["files"] == m1["files"]) if committed and committed["seed"] == seed
        else None,
        "sha256_submission_csv": m1["files"].get(f"generated/{scale}/csv/submission.csv"),
    }


# ------------------------------------------------------------------- driver
def run_validation(scale: str, data_root: Path = DATA_DIR, run_repro: bool = False,
                   settings: dict | None = None, rebuild_db: bool = True) -> dict:
    settings = settings or load_settings()
    data_root = Path(data_root)
    db_path = db_path_for(scale, data_root)
    if rebuild_db or not db_path.exists():
        init_db(scale, data_root, db_path)
    conn = connect(db_path)
    manifest = json.loads((data_root / "generated" / scale / "manifest.json").read_text())
    checks = structural_checks(conn, settings, settings["scales"][scale]) + workflow_checks(conn, settings)
    detected = detect_anomalies(conn)
    evaluation = evaluate(conn, detected, data_root / "ground_truth" / scale)
    summary = {
        "scale": scale, "seed": manifest["seed"], "as_of": settings["as_of"],
        "row_counts": manifest["row_counts"],
        "checks": checks,
        "checks_passed": sum(c["passed"] for c in checks), "checks_total": len(checks),
        "anomalies_detected": {k: [list(r) for r in v] for k, v in detected.items()},
        "evaluation": evaluation,
        "reconciliation": reconciliation(conn),
        "audit": audit_completeness(conn),
        "metrics": compute_metrics(conn, settings, detected),
    }
    if run_repro:
        summary["reproducibility"] = reproducibility(scale, manifest["seed"], settings)
    ok = (summary["checks_passed"] == summary["checks_total"]
          and all(v["passed"] for v in evaluation["anomaly_detection"].values())
          and all(summary["reconciliation"]["balanced"].values()))
    if run_repro:
        r = summary["reproducibility"]
        ok = ok and r["run1_equals_run2"] and r["different_seed_changes_output"] \
            and r["committed_data_matches_code"] is not False
    summary["overall_passed"] = ok
    conn.close()
    return summary


def write_report(summary: dict, out_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = summary["scale"]
    json_path = out_dir / f"validation_summary_{scale}.json"
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    m, ev, rec, audit = summary["metrics"], summary["evaluation"], summary["reconciliation"], summary["audit"]
    lines = [
        f"# Validation report ({scale} scale, seed {summary['seed']})", "",
        f"**Overall: {'PASS' if summary['overall_passed'] else 'FAIL'}** - "
        f"{summary['checks_passed']}/{summary['checks_total']} operational checks passed.", "",
        "Generated by `python -m src.validation --scale " + scale + " --run-all`. Data is fully synthetic.", "",
        "## Row counts", "", "| Table | Rows |", "|---|---:|",
        *[f"| {t} | {n} |" for t, n in summary["row_counts"].items()], "",
        "## Operational checks", "", "| Check | Category | Result | Violations |", "|---|---|---|---:|",
        *[f"| {c['name']} | {c['category']} | {'pass' if c['passed'] else 'FAIL'} | {c['violations']} |"
          for c in summary["checks"]], "",
        "## Labelled negative scenarios (detector vs separate ground truth)", "",
        "| Issue type | Expected | Detected | Missed | Unexpected | Result |", "|---|---:|---:|---:|---:|---|",
        *[f"| {k} | {v['expected']} | {v['detected']} | {len(v['missed'])} | {len(v['unexpected'])} | "
          f"{'pass' if v['passed'] else 'FAIL'} |" for k, v in ev["anomaly_detection"].items()], "",
        "Detected records: " + "; ".join(f"{k}: {', '.join(r[1] for r in v)}"
                                        for k, v in summary["anomalies_detected"].items()), "",
        "## Reconciliation", "",
        *[f"- {k}: {v}" for k, v in rec.items() if k not in ("balanced", "explanation")],
        *[f"- balanced - {k}: {'yes' if v else 'NO'}" for k, v in rec["balanced"].items()], "",
        rec["explanation"], "",
        "## Metrics", "",
        f"- Flag rate: {m['flag_rate']:.1%}",
        f"- Case creation rate (cases / flags): {m['case_creation_rate']:.1%}",
        f"- False-positive rate of reviewed flags (ground truth): {ev['false_positive_rate_of_reviewed_flags']:.1%} "
        f"of {ev['reviewed_cases_with_ground_truth']} reviewed cases",
        f"- Adverse final outcomes on false-positive flags: {ev['adverse_final_outcomes_on_false_positive_flags']} "
        f"of {ev['adverse_final_outcomes']}",
        f"- Decision turnaround (days): mean {m['decision_turnaround_days']['mean']}, "
        f"median {m['decision_turnaround_days']['median']}, p90 {m['decision_turnaround_days']['p90']}",
        f"- Appeal rate (appeals / adverse first decisions): {m['appeal_rate']:.1%}",
        f"- Appeal success rate (overturned or modified / decided appeals): {m['appeal_success_rate']:.1%}",
        f"- Independent review compliance: {m['independent_review_compliance']:.1%}",
        f"- Conflict assignment violations: {m['conflict_assignment_violations']} "
        f"(guard blocked {m['conflict_assignments_blocked_by_guard']} conflicted attempts)",
        f"- Audit completeness: {audit['audit_completeness']:.2%} "
        f"({audit['with_event']}/{audit['required_state_changes']}); missing: {audit['missing']}",
        f"- Reviewer workload (assignments): min {m['reviewer_workload']['min']}, max {m['reviewer_workload']['max']}, "
        f"mean {m['reviewer_workload']['mean']}",
        f"- Case status: {m['case_status_counts']}",
        f"- Final outcomes: {m['final_outcome_counts']}", "",
        "Audit completeness and the violation counts are below 100% / above zero only because of the labelled "
        "negative scenarios; the evaluation table above shows every one is accounted for.", "",
    ]
    if "reproducibility" in summary:
        r = summary["reproducibility"]
        lines += ["## Reproducibility", "",
                  f"- Same seed ({r['seed']}) generated twice: identical SHA-256 for all {r['files_compared']} files: "
                  f"{'yes' if r['run1_equals_run2'] else 'NO'}",
                  f"- Different seed changes the output: {'yes' if r['different_seed_changes_output'] else 'NO'}",
                  f"- Committed data in `data/` matches what the code generates: {r['committed_data_matches_code']}",
                  f"- SHA-256 of submission.csv: `{r['sha256_submission_csv']}`", ""]
    md_path = out_dir / f"validation_report_{scale}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", choices=["starter", "full"], default="starter")
    parser.add_argument("--run-all", action="store_true", help="also run the reproducibility (double generation) check")
    args = parser.parse_args()
    summary = run_validation(args.scale, run_repro=args.run_all)
    json_path, md_path = write_report(summary)
    failed = [c["name"] for c in summary["checks"] if not c["passed"]]
    print(f"Operational checks: {summary['checks_passed']}/{summary['checks_total']} passed"
          + (f" (failed: {failed})" if failed else ""))
    for k, v in summary["evaluation"]["anomaly_detection"].items():
        print(f"  {k:<42} expected {v['expected']}  detected {v['detected']}  {'ok' if v['passed'] else 'MISMATCH'}")
    if "reproducibility" in summary:
        r = summary["reproducibility"]
        print(f"Reproducibility: same seed identical={r['run1_equals_run2']}, "
              f"different seed differs={r['different_seed_changes_output']}, "
              f"committed data matches={r['committed_data_matches_code']}")
    print(f"Audit completeness: {summary['audit']['audit_completeness']:.2%}")
    print(f"OVERALL: {'PASS' if summary['overall_passed'] else 'FAIL'}")
    print(f"Wrote {json_path.relative_to(OUTPUT_DIR.parent)} and {md_path.relative_to(OUTPUT_DIR.parent)}")
    sys.exit(0 if summary["overall_passed"] else 1)


if __name__ == "__main__":
    main()
