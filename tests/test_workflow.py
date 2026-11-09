"""Unit tests for the guarded workflow: every business rule must block what it should."""
import pytest

from src import rules
from src.common import connect, create_schema, load_settings
from src.workflow import CaseWorkflow, ConflictOfInterestError, WorkflowError

SETTINGS = load_settings()
CASE = "CASE-2026-000001"
REPORT = "SIM-SUB-2026-000001-01"
RATIONALE = "Matched passages are not quoted or cited; reviewer judgement after evidence review."


@pytest.fixture
def wf():
    conn = connect()
    create_schema(conn)
    conn.execute("INSERT INTO learner VALUES ('LRN-2026-000001','Synthetic Learner 000001','l1@learners.example.org','BSc',2025,1)")
    conn.execute("INSERT INTO assessment VALUES ('ASM-2026-0001','ENV101','Essay 1','Environmental Science','essay',0,"
                 "'2026-03-10T23:59:00Z')")
    rule = SETTINGS["integrity_rules"][1]
    conn.execute("INSERT INTO integrity_rule VALUES (?,?,?,?,?,?,?,?,?)", tuple(rule.values()))
    for i, role in [(1, "academic_reviewer"), (2, "academic_reviewer"), (3, "integrity_officer"), (4, "integrity_officer")]:
        conn.execute("INSERT INTO reviewer VALUES (?,?,?,?,?)",
                     (f"REV-2026-000{i}", f"Synthetic Reviewer {i}", f"r{i}@staff.example.org", "Dept", role))
    conn.execute("INSERT INTO reviewer_conflict VALUES ('CONF-2026-000001','REV-2026-0002','LRN-2026-000001',"
                 "'personal tutor','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO submission VALUES ('SUB-2026-000001','LRN-2026-000001','ASM-2026-0001',"
                 "'2026-03-10T20:00:00Z',2000)")
    conn.execute("INSERT INTO similarity_report VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (REPORT, "SUB-2026-000001", rule["integrity_rule_id"], "2026-03-11T08:00:00Z", 72.5, 6, 0.04, 0,
                  "priority", 1, "SYNTHETIC://x"))
    return CaseWorkflow(conn, SETTINGS)


def open_and_assign(wf):
    wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")
    wf.add_evidence(CASE, "similarity_report", "SYNTHETIC://r", "report", "SYSTEM-TRIAGE", "2026-03-11T10:00:00Z", REPORT)
    wf.assign_reviewer(CASE, "REV-2026-0001", "primary", "2026-03-11T12:00:00Z")


def decide(wf, outcome="confirmed_breach", at="2026-03-15T12:00:00Z"):
    return wf.record_decision(CASE, "REV-2026-0001", outcome, RATIONALE, at)


def test_flag_opens_case_without_any_decision(wf):
    wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")
    assert wf.conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0
    assert wf.conn.execute("SELECT status FROM integrity_case").fetchone()[0] == "open"


def test_unflagged_report_cannot_open_case(wf):
    wf.conn.execute("UPDATE similarity_report SET is_flagged=0, score_band='none', similarity_score=5")
    with pytest.raises(WorkflowError):
        wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")


def test_decision_requires_assigned_human_reviewer(wf):
    wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")
    wf.add_evidence(CASE, "similarity_report", "SYNTHETIC://r", "report", "SYSTEM-TRIAGE", "2026-03-11T10:00:00Z")
    with pytest.raises(WorkflowError):
        decide(wf)  # nobody assigned yet -> no automatic guilt


def test_conflicted_reviewer_is_blocked_and_logged(wf):
    wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")
    with pytest.raises(ConflictOfInterestError):
        wf.assign_reviewer(CASE, "REV-2026-0002", "primary", "2026-03-11T12:00:00Z")
    row = wf.conn.execute("SELECT assignment_status, status_reason FROM case_reviewer").fetchone()
    assert row[0] == "blocked_conflict" and "CONF-2026-000001" in row[1]
    events = [r[0] for r in wf.conn.execute("SELECT event_type FROM case_event ORDER BY event_seq")]
    assert events == ["case_opened", "reviewer_assignment_blocked"]


def test_decision_requires_rationale(wf):
    open_and_assign(wf)
    with pytest.raises(WorkflowError):
        wf.record_decision(CASE, "REV-2026-0001", "confirmed_breach", "too short", "2026-03-15T12:00:00Z")


def test_cannot_close_without_decision(wf):
    open_and_assign(wf)
    with pytest.raises(WorkflowError):
        wf.close_case(CASE, "2026-03-20T00:00:00Z")


def test_adverse_case_stays_open_during_appeal_window(wf):
    open_and_assign(wf)
    decide(wf)
    with pytest.raises(WorkflowError):
        wf.close_case(CASE, "2026-03-20T00:00:00Z")
    wf.close_case(CASE, "2026-04-01T00:00:00Z")  # after the 14-day window
    assert wf.conn.execute("SELECT status FROM integrity_case").fetchone()[0] == "closed"


def test_appeal_window_enforced(wf):
    open_and_assign(wf)
    decide(wf)
    with pytest.raises(WorkflowError):
        wf.submit_appeal(CASE, "late appeal", "2026-04-15T00:00:00Z")


def test_non_adverse_outcome_cannot_be_appealed(wf):
    open_and_assign(wf)
    decide(wf, outcome="no_breach")
    with pytest.raises(WorkflowError):
        wf.submit_appeal(CASE, "nothing to appeal", "2026-03-16T00:00:00Z")


def test_appeal_reviewer_must_be_independent_and_unconflicted(wf):
    open_and_assign(wf)
    decide(wf)
    appeal_id = wf.submit_appeal(CASE, "grounds", "2026-03-16T00:00:00Z")
    with pytest.raises(WorkflowError):
        wf.assign_appeal_reviewer(appeal_id, "REV-2026-0001", "2026-03-16T06:00:00Z")  # original decision-maker
    with pytest.raises(ConflictOfInterestError):
        wf.assign_appeal_reviewer(appeal_id, "REV-2026-0002", "2026-03-16T06:00:00Z")  # declared conflict
    with pytest.raises(WorkflowError):
        wf.record_appeal_outcome(appeal_id, "upheld", RATIONALE, "2026-03-17T00:00:00Z")  # no reviewer yet
    with pytest.raises(WorkflowError):
        wf.close_case(CASE, "2026-03-17T00:00:00Z")  # appeal pending
    wf.assign_appeal_reviewer(appeal_id, "REV-2026-0003", "2026-03-16T06:00:00Z")
    new_decision = wf.record_appeal_outcome(appeal_id, "overturned", RATIONALE, "2026-03-20T00:00:00Z", "no_breach")
    wf.close_case(CASE, "2026-03-20T02:00:00Z")
    rows = wf.conn.execute("SELECT decision_id, outcome, supersedes_decision_id FROM decision ORDER BY decision_round").fetchall()
    assert [r[1] for r in rows] == ["confirmed_breach", "no_breach"]  # original kept, not overwritten
    assert rows[1][0] == new_decision and rows[1][2] == rows[0][0]


def test_evidence_is_append_only_and_audited(wf):
    open_and_assign(wf)
    decide(wf)
    evidence_id = wf.add_evidence(CASE, "reviewer_note", "SYNTHETIC://n", "late note", "REV-2026-0001",
                                  "2026-03-15T13:00:00Z")
    assert evidence_id.endswith("-002")
    assert wf.conn.execute("SELECT COUNT(*) FROM case_event WHERE entity_id=?", (evidence_id,)).fetchone()[0] == 1


def test_actions_cannot_go_back_in_time(wf):
    open_and_assign(wf)
    with pytest.raises(WorkflowError):
        wf.record_decision(CASE, "REV-2026-0001", "confirmed_breach", RATIONALE, "2026-03-11T11:00:00Z")


def test_status_is_derived_from_events(wf):
    open_and_assign(wf)
    decide(wf)
    events = [r[0] for r in wf.conn.execute("SELECT event_type FROM case_event ORDER BY event_seq")]
    assert rules.derive_status(events) == wf.conn.execute("SELECT status FROM integrity_case").fetchone()[0] == "decided"
