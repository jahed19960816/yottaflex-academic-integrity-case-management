"""Business rules, enums and derivations.

Key principle: a similarity score is a *review signal*. It can open a case for human
review; it can never by itself produce a finding of misconduct.
"""
from __future__ import annotations

import random

SCORE_BANDS = ("none", "low", "standard", "priority")
BAND_RANK = {"none": 0, "low": 1, "standard": 2, "priority": 3}

REVIEW_OUTCOMES = (
    "no_breach",
    "insufficient_evidence",
    "educational_warning",
    "confirmed_breach",
    "referred_for_further_review",
)
# Outcomes that harm the learner and can therefore be appealed.
ADVERSE_OUTCOMES = frozenset({"educational_warning", "confirmed_breach"})

# Appeal outcome describes what happened to the ORIGINAL decision:
#   upheld = original decision stands (appeal unsuccessful)
#   overturned = original decision replaced by no_breach (appeal successful)
#   modified = original decision replaced by a lesser outcome (appeal successful)
#   rejected_out_of_scope = appeal not admissible, original decision stands
APPEAL_OUTCOMES = ("upheld", "overturned", "modified", "rejected_out_of_scope", "pending")
SUCCESSFUL_APPEAL_OUTCOMES = frozenset({"overturned", "modified"})

CASE_STATUSES = ("open", "under_review", "decided", "appeal_open", "closed")
EVENT_TYPES = (
    "case_opened",
    "reviewer_assigned",
    "reviewer_assignment_blocked",
    "evidence_added",
    "decision_recorded",
    "appeal_submitted",
    "appeal_reviewer_assigned",
    "appeal_decided",
    "case_closed",
)
EVIDENCE_TYPES = (
    "similarity_report",
    "matched_source_excerpt",
    "learner_statement",
    "draft_history",
    "reviewer_note",
)
ASSIGNMENT_ROLES = ("primary", "secondary", "appeal")
ASSIGNMENT_STATUSES = ("active", "completed", "blocked_conflict")
REVIEWER_ROLES = ("academic_reviewer", "integrity_officer")
DECISION_SOURCES = ("human_review", "automated")
ASSESSMENT_TYPES = ("essay", "report", "lab_report", "code")

# Status is derived from the event history, never set at random.
STATUS_AFTER_EVENT = {
    "case_opened": "open",
    "reviewer_assigned": "under_review",
    "decision_recorded": "decided",
    "appeal_submitted": "appeal_open",
    "appeal_decided": "decided",
    "case_closed": "closed",
}

# What a successful appeal changes the outcome to.
MODIFIED_OUTCOME = {
    "confirmed_breach": "educational_warning",
    "educational_warning": "insufficient_evidence",
}


def active_rule(rules: list[dict], at_iso: str) -> dict:
    """Return the integrity rule in force at a timestamp (effective_to is exclusive)."""
    for rule in rules:
        if rule["effective_from"] <= at_iso and (rule["effective_to"] is None or at_iso < rule["effective_to"]):
            return rule
    raise ValueError(f"No integrity rule in force at {at_iso}")


def score_band(score: float, rule: dict) -> str:
    if score >= rule["priority_threshold"]:
        return "priority"
    if score >= rule["standard_threshold"]:
        return "standard"
    if score >= rule["low_threshold"]:
        return "low"
    return "none"


def derive_status(event_types: list[str]) -> str | None:
    status = None
    for event_type in event_types:
        status = STATUS_AFTER_EVENT.get(event_type, status)
    return status


def adjusted_signal(report: dict) -> float:
    """Score after discounting legitimately quoted material and shared templates."""
    return (
        report["similarity_score"]
        - 60 * report["quoted_material_share"]
        - (20 if report["common_template_indicator"] else 0)
    )


def simulate_review_outcome(report: dict, rng: random.Random) -> str:
    """Simulated human reviewer judgement.

    Uses ONLY observable report features (never ground truth). Includes a little noise
    because real reviewers are not perfectly consistent.
    """
    signal = adjusted_signal(report)
    if report["matched_source_count"] >= 4 and signal >= 45:
        ladder_pos = 3  # confirmed_breach
    elif signal >= 30:
        ladder_pos = 2  # educational_warning
    elif signal >= 18:
        ladder_pos = 1  # insufficient_evidence
    else:
        ladder_pos = 0  # no_breach
    roll = rng.random()
    if roll < 0.05:
        return "referred_for_further_review"
    if roll < 0.12:
        ladder_pos = max(0, min(3, ladder_pos + rng.choice((-1, 1))))
    return ("no_breach", "insufficient_evidence", "educational_warning", "confirmed_breach")[ladder_pos]


def simulate_appeal_outcome(report: dict, original_outcome: str, rng: random.Random) -> tuple[str, str | None]:
    """Return (appeal_outcome, revised_decision_outcome or None)."""
    mitigating = report["quoted_material_share"] >= 0.3 or report["common_template_indicator"] == 1
    roll = rng.random()
    if roll < 0.08:
        return "rejected_out_of_scope", None
    success_p = 0.55 if mitigating else 0.20
    if roll < 0.08 + success_p:
        if mitigating or original_outcome == "educational_warning":
            return "overturned", "no_breach"
        return "modified", MODIFIED_OUTCOME[original_outcome]
    return "upheld", None
