"""Guarded case workflow on top of the SQLite schema.

Every state change goes through one of these methods. Each method checks the business
rules first, writes the change, and appends a CaseEvent, so the timeline is complete.
Timestamps are passed in explicitly, which keeps data generation deterministic.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from . import rules
from .common import parse_iso


class WorkflowError(Exception):
    """A business rule blocked the requested action."""


class ConflictOfInterestError(WorkflowError):
    """The reviewer has a declared conflict with the learner."""


class CaseWorkflow:
    def __init__(self, conn: sqlite3.Connection, settings: dict):
        self.conn = conn
        self.appeal_window = timedelta(days=settings["appeal_window_days"])
        self.min_rationale = settings["min_rationale_chars"]

    # ------------------------------------------------------------------ helpers
    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _case(self, case_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM integrity_case WHERE case_id = ?", (case_id,))
        if row is None:
            raise WorkflowError(f"Unknown case {case_id}")
        return row

    def _case_learner(self, case_id: str) -> str:
        return self._one(
            "SELECT s.learner_id FROM integrity_case c JOIN submission s USING (submission_id) WHERE c.case_id = ?",
            (case_id,),
        )["learner_id"]

    def _set_status(self, case_id: str, status: str) -> None:
        self.conn.execute("UPDATE integrity_case SET status = ? WHERE case_id = ?", (status, case_id))

    def _last_event_at(self, case_id: str) -> str | None:
        row = self._one("SELECT MAX(event_at) AS t FROM case_event WHERE case_id = ?", (case_id,))
        return row["t"]

    def _check_time(self, case_id: str, at: str) -> None:
        last = self._last_event_at(case_id)
        if last is not None and at < last:
            raise WorkflowError(f"{case_id}: action at {at} is earlier than the last recorded event {last}")

    def event(self, case_id: str, event_type: str, at: str, actor_id: str,
              entity_type: str | None = None, entity_id: str | None = None, details: dict | None = None) -> str:
        seq = self._one("SELECT COUNT(*) AS n FROM case_event WHERE case_id = ?", (case_id,))["n"] + 1
        event_id = f"EVT-{case_id}-{seq:04d}"
        self.conn.execute(
            "INSERT INTO case_event VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, case_id, seq, event_type, at, actor_id, entity_type, entity_id,
             json.dumps(details, sort_keys=True) if details else None),
        )
        return event_id

    def has_conflict(self, reviewer_id: str, learner_id: str, at: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM reviewer_conflict WHERE reviewer_id = ? AND learner_id = ? AND declared_at <= ?",
            (reviewer_id, learner_id, at),
        )

    def _next_assignment_id(self, case_id: str) -> str:
        n = self._one("SELECT COUNT(*) AS n FROM case_reviewer WHERE case_id = ?", (case_id,))["n"] + 1
        return f"ASG-{case_id}-{n:02d}"

    def _record_blocked(self, case_id: str, reviewer_id: str, role: str, at: str, conflict: sqlite3.Row) -> None:
        assignment_id = self._next_assignment_id(case_id)
        reason = f"Conflict of interest {conflict['conflict_id']}: {conflict['reason']}"
        self.conn.execute(
            "INSERT INTO case_reviewer VALUES (?,?,?,?,?,?,?,?)",
            (assignment_id, case_id, reviewer_id, role, "blocked_conflict", at, at, reason),
        )
        self.event(case_id, "reviewer_assignment_blocked", at, "SYSTEM-COI-CHECK", "case_reviewer", assignment_id,
                   {"reviewer_id": reviewer_id, "conflict_id": conflict["conflict_id"]})

    def latest_decision(self, case_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM decision WHERE case_id = ? ORDER BY decision_round DESC LIMIT 1", (case_id,)
        )

    # ------------------------------------------------------------------ actions
    def open_case(self, case_id: str, report_id: str, at: str, opened_by: str = "SYSTEM-TRIAGE") -> None:
        """A flag opens a case for HUMAN review. It never creates a decision."""
        report = self._one("SELECT * FROM similarity_report WHERE similarity_report_id = ?", (report_id,))
        if report is None:
            raise WorkflowError(f"Unknown similarity report {report_id}")
        if not report["is_flagged"]:
            raise WorkflowError(f"{report_id} is below the review threshold; no case may be opened")
        if at < report["generated_at"]:
            raise WorkflowError("A case cannot be opened before its similarity report exists")
        self.conn.execute(
            "INSERT INTO integrity_case VALUES (?,?,?,?,?,?,?,?)",
            (case_id, report["submission_id"], report_id, report["integrity_rule_id"],
             report["score_band"], at, "open", None),
        )
        self.event(case_id, "case_opened", at, opened_by, "similarity_report", report_id,
                   {"similarity_score": report["similarity_score"], "score_band": report["score_band"],
                    "note": "Flag opens human review; it is not a finding of misconduct."})

    def assign_reviewer(self, case_id: str, reviewer_id: str, role: str, at: str) -> str:
        if role not in ("primary", "secondary"):
            raise WorkflowError("Use assign_appeal_reviewer for appeal reviewers")
        case = self._case(case_id)
        if case["status"] not in ("open", "under_review"):
            raise WorkflowError(f"{case_id} is {case['status']}; reviewers can only be assigned before a decision")
        self._check_time(case_id, at)
        conflict = self.has_conflict(reviewer_id, self._case_learner(case_id), at)
        if conflict is not None:
            self._record_blocked(case_id, reviewer_id, role, at, conflict)
            raise ConflictOfInterestError(f"{reviewer_id} has conflict {conflict['conflict_id']} with this learner")
        if self._one("SELECT 1 FROM case_reviewer WHERE case_id=? AND reviewer_id=? AND assignment_status='active'",
                     (case_id, reviewer_id)):
            raise WorkflowError(f"{reviewer_id} is already assigned to {case_id}")
        assignment_id = self._next_assignment_id(case_id)
        self.conn.execute(
            "INSERT INTO case_reviewer VALUES (?,?,?,?,?,?,?,?)",
            (assignment_id, case_id, reviewer_id, role, "active", at, None, None),
        )
        self.event(case_id, "reviewer_assigned", at, "SYSTEM-ASSIGNMENT", "case_reviewer", assignment_id,
                   {"reviewer_id": reviewer_id, "role": role})
        self._set_status(case_id, "under_review")
        return assignment_id

    def add_evidence(self, case_id: str, evidence_type: str, evidence_ref: str, description: str,
                     added_by: str, at: str, similarity_report_id: str | None = None) -> str:
        """Append-only: new evidence never replaces what reviewers saw earlier."""
        if evidence_type not in rules.EVIDENCE_TYPES:
            raise WorkflowError(f"Unknown evidence type {evidence_type}")
        case = self._case(case_id)
        if case["status"] == "closed":
            raise WorkflowError(f"{case_id} is closed; evidence cannot be added")
        if at < case["opened_at"]:
            raise WorkflowError("Evidence cannot pre-date the case")
        self._check_time(case_id, at)
        seq = self._one("SELECT COUNT(*) AS n FROM evidence_item WHERE case_id = ?", (case_id,))["n"] + 1
        evidence_id = f"EVD-{case_id}-{seq:03d}"
        self.conn.execute(
            "INSERT INTO evidence_item VALUES (?,?,?,?,?,?,?,?,?)",
            (evidence_id, case_id, similarity_report_id, evidence_type, evidence_ref, description, added_by, at, seq),
        )
        self.event(case_id, "evidence_added", at, added_by, "evidence_item", evidence_id,
                   {"evidence_type": evidence_type, "case_evidence_seq": seq})
        return evidence_id

    def record_decision(self, case_id: str, reviewer_id: str, outcome: str, rationale: str, at: str) -> str:
        if outcome not in rules.REVIEW_OUTCOMES:
            raise WorkflowError(f"Unknown outcome {outcome}")
        if rationale is None or len(rationale.strip()) < self.min_rationale:
            raise WorkflowError(f"A decision needs a rationale of at least {self.min_rationale} characters")
        case = self._case(case_id)
        if case["status"] != "under_review":
            raise WorkflowError(f"{case_id} is {case['status']}; a first decision needs an assigned human reviewer")
        self._check_time(case_id, at)
        if not self._one(
            "SELECT 1 FROM case_reviewer WHERE case_id=? AND reviewer_id=? AND assignment_role IN ('primary','secondary') "
            "AND assignment_status='active' AND assigned_at <= ?", (case_id, reviewer_id, at)):
            raise WorkflowError(f"{reviewer_id} is not an active reviewer on {case_id}")
        if not self._one("SELECT 1 FROM evidence_item WHERE case_id=? AND added_at <= ?", (case_id, at)):
            raise WorkflowError("A decision requires the reviewer to have evidence to inspect")
        return self._insert_decision(case_id, reviewer_id, outcome, rationale, at, None)

    def _insert_decision(self, case_id, reviewer_id, outcome, rationale, at, supersedes) -> str:
        round_no = self._one("SELECT COUNT(*) AS n FROM decision WHERE case_id = ?", (case_id,))["n"] + 1
        decision_id = f"DEC-{case_id}-{round_no:02d}"
        self.conn.execute(
            "INSERT INTO decision VALUES (?,?,?,?,?,?,?,?,?)",
            (decision_id, case_id, reviewer_id, "human_review", outcome, rationale, at, round_no, supersedes),
        )
        self.event(case_id, "decision_recorded", at, reviewer_id, "decision", decision_id,
                   {"outcome": outcome, "decision_round": round_no})
        self._set_status(case_id, "decided")
        return decision_id

    def submit_appeal(self, case_id: str, grounds: str, at: str) -> str:
        case = self._case(case_id)
        if case["status"] != "decided":
            raise WorkflowError(f"{case_id} is {case['status']}; an appeal needs a decided case")
        decision = self.latest_decision(case_id)
        if decision["outcome"] not in rules.ADVERSE_OUTCOMES:
            raise WorkflowError(f"Outcome {decision['outcome']} is not adverse and cannot be appealed")
        if self._one("SELECT 1 FROM appeal WHERE decision_id = ?", (decision["decision_id"],)):
            raise WorkflowError("This decision has already been appealed")
        decided = parse_iso(decision["decided_at"])
        if not (decided < parse_iso(at) <= decided + self.appeal_window):
            raise WorkflowError("Appeal is outside the appeal window")
        self._check_time(case_id, at)
        n = self._one("SELECT COUNT(*) AS n FROM appeal WHERE case_id = ?", (case_id,))["n"] + 1
        appeal_id = f"APL-{case_id}-{n:02d}"
        learner_id = self._case_learner(case_id)
        self.conn.execute(
            "INSERT INTO appeal VALUES (?,?,?,?,?,?,?,?,?,?)",
            (appeal_id, case_id, decision["decision_id"], at, grounds, None, None, "pending", None, None),
        )
        self.event(case_id, "appeal_submitted", at, learner_id, "appeal", appeal_id,
                   {"decision_id": decision["decision_id"]})
        self._set_status(case_id, "appeal_open")
        return appeal_id

    def assign_appeal_reviewer(self, appeal_id: str, reviewer_id: str, at: str) -> str:
        appeal = self._one("SELECT * FROM appeal WHERE appeal_id = ?", (appeal_id,))
        if appeal is None or appeal["outcome"] != "pending":
            raise WorkflowError(f"{appeal_id} is not a pending appeal")
        if appeal["appeal_reviewer_id"] is not None:
            raise WorkflowError(f"{appeal_id} already has an appeal reviewer")
        case_id = appeal["case_id"]
        self._check_time(case_id, at)
        involved = self._one(
            "SELECT 1 FROM case_reviewer WHERE case_id=? AND reviewer_id=? AND assignment_status <> 'blocked_conflict'",
            (case_id, reviewer_id))
        deciders = {r["reviewer_id"] for r in self.conn.execute(
            "SELECT reviewer_id FROM decision WHERE case_id = ?", (case_id,))}
        if involved or reviewer_id in deciders:
            raise WorkflowError(f"{reviewer_id} was involved in the original review and is not independent")
        conflict = self.has_conflict(reviewer_id, self._case_learner(case_id), at)
        if conflict is not None:
            self._record_blocked(case_id, reviewer_id, "appeal", at, conflict)
            raise ConflictOfInterestError(f"{reviewer_id} has conflict {conflict['conflict_id']} with this learner")
        assignment_id = self._next_assignment_id(case_id)
        self.conn.execute(
            "INSERT INTO case_reviewer VALUES (?,?,?,?,?,?,?,?)",
            (assignment_id, case_id, reviewer_id, "appeal", "active", at, None, None),
        )
        self.conn.execute(
            "UPDATE appeal SET appeal_reviewer_id = ?, reviewer_assigned_at = ? WHERE appeal_id = ?",
            (reviewer_id, at, appeal_id),
        )
        self.event(case_id, "appeal_reviewer_assigned", at, "SYSTEM-ASSIGNMENT", "appeal", appeal_id,
                   {"reviewer_id": reviewer_id, "case_reviewer_id": assignment_id})
        return assignment_id

    def record_appeal_outcome(self, appeal_id: str, outcome: str, rationale: str, at: str,
                              revised_outcome: str | None = None) -> str | None:
        appeal = self._one("SELECT * FROM appeal WHERE appeal_id = ?", (appeal_id,))
        if appeal is None or appeal["outcome"] != "pending":
            raise WorkflowError(f"{appeal_id} is not a pending appeal")
        if outcome not in rules.APPEAL_OUTCOMES or outcome == "pending":
            raise WorkflowError(f"Invalid appeal outcome {outcome}")
        if appeal["appeal_reviewer_id"] is None or appeal["reviewer_assigned_at"] > at:
            raise WorkflowError("An appeal cannot be decided before an independent reviewer is assigned")
        if rationale is None or len(rationale.strip()) < self.min_rationale:
            raise WorkflowError(f"An appeal outcome needs a rationale of at least {self.min_rationale} characters")
        case_id = appeal["case_id"]
        self._check_time(case_id, at)
        new_decision_id = None
        if outcome in rules.SUCCESSFUL_APPEAL_OUTCOMES:
            if revised_outcome is None:
                raise WorkflowError("A successful appeal must state the revised outcome")
            self.conn.execute(
                "UPDATE appeal SET outcome=?, outcome_rationale=?, decided_at=? WHERE appeal_id=?",
                (outcome, rationale, at, appeal_id))
            self.event(case_id, "appeal_decided", at, appeal["appeal_reviewer_id"], "appeal", appeal_id,
                       {"outcome": outcome})
            # The original decision is kept; the revised one supersedes it.
            new_decision_id = self._insert_decision(
                case_id, appeal["appeal_reviewer_id"], revised_outcome,
                f"Appeal {appeal_id} {outcome}: {rationale}", at, appeal["decision_id"])
        else:
            self.conn.execute(
                "UPDATE appeal SET outcome=?, outcome_rationale=?, decided_at=? WHERE appeal_id=?",
                (outcome, rationale, at, appeal_id))
            self.event(case_id, "appeal_decided", at, appeal["appeal_reviewer_id"], "appeal", appeal_id,
                       {"outcome": outcome})
            self._set_status(case_id, "decided")
        return new_decision_id

    def close_case(self, case_id: str, at: str, closed_by: str = "SYSTEM-CASE-ADMIN") -> None:
        case = self._case(case_id)
        if case["status"] != "decided":
            raise WorkflowError(f"{case_id} is {case['status']}; only decided cases can close")
        self._check_time(case_id, at)
        decision = self.latest_decision(case_id)
        if decision is None or decision["decision_source"] != "human_review" or \
                len((decision["rationale"] or "").strip()) < self.min_rationale:
            raise WorkflowError("A case cannot close without a human decision and rationale")
        if self._one("SELECT 1 FROM appeal WHERE case_id=? AND outcome='pending'", (case_id,)):
            raise WorkflowError("A case cannot close while an appeal is pending")
        for appeal in self.conn.execute("SELECT * FROM appeal WHERE case_id = ?", (case_id,)).fetchall():
            if appeal["appeal_reviewer_id"] is None:
                raise WorkflowError(f"{appeal['appeal_id']} was not independently reviewed")
        appealed = self._one("SELECT 1 FROM appeal WHERE decision_id = ?", (decision["decision_id"],))
        is_appeal_result = decision["supersedes_decision_id"] is not None
        if decision["outcome"] in rules.ADVERSE_OUTCOMES and not appealed and not is_appeal_result:
            if parse_iso(at) <= parse_iso(decision["decided_at"]) + self.appeal_window:
                raise WorkflowError("Adverse decision: the case stays open until the appeal window has passed")
        self.conn.execute(
            "UPDATE case_reviewer SET assignment_status='completed', unassigned_at=? "
            "WHERE case_id=? AND assignment_status='active'", (at, case_id))
        self.conn.execute("UPDATE integrity_case SET status='closed', closed_at=? WHERE case_id=?", (at, case_id))
        self.event(case_id, "case_closed", at, closed_by, "decision", decision["decision_id"],
                   {"final_outcome": decision["outcome"]})
