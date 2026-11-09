"""Walk through one case end to end and show each guard working.

Usage:
    python -m src.demo
Runs on a fresh in-memory database, so it never changes the generated data.
"""
from __future__ import annotations

from . import rules
from .common import connect, create_schema, load_settings
from .workflow import CaseWorkflow, ConflictOfInterestError, WorkflowError

CASE = "CASE-2026-000001"
REPORT = "SIM-SUB-2026-000001-01"
GOOD_RATIONALE = ("Six matched passages are unquoted and uncited (quoted share 4%). The learner statement does not "
                  "explain them. Similarity score used as a signal only.")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def expect_block(label: str, fn) -> None:
    try:
        fn()
    except (WorkflowError, ConflictOfInterestError) as exc:
        print(f"    BLOCKED as expected - {label}: {exc}")
    else:
        raise SystemExit(f"Guard failed: {label} was allowed")


def main() -> None:
    settings = load_settings()
    conn = connect()
    create_schema(conn)
    wf = CaseWorkflow(conn, settings)
    rule = settings["integrity_rules"][1]

    step(1, "Create a learner, an assessment and a submission (synthetic)")
    conn.execute("INSERT INTO learner VALUES ('LRN-2026-000001','Synthetic Learner 000001',"
                 "'learner000001@learners.example.org','BSc Environmental Science',2025,1)")
    conn.execute("INSERT INTO assessment VALUES ('ASM-2026-0001','ENV101','Essay 1','Environmental Science','essay',0,"
                 "'2026-03-10T23:59:00Z')")
    conn.execute("INSERT INTO integrity_rule VALUES (?,?,?,?,?,?,?,?,?)", tuple(rule.values()))
    for i, role in [(1, "academic_reviewer"), (2, "academic_reviewer"), (3, "integrity_officer")]:
        conn.execute("INSERT INTO reviewer VALUES (?,?,?,?,?)", (f"REV-2026-000{i}", f"Synthetic Reviewer 000{i}",
                                                                 f"reviewer000{i}@staff.example.org", "Env", role))
    conn.execute("INSERT INTO reviewer_conflict VALUES ('CONF-2026-000001','REV-2026-0001','LRN-2026-000001',"
                 "'personal tutor','2026-01-05T09:00:00Z')")
    conn.execute("INSERT INTO submission VALUES ('SUB-2026-000001','LRN-2026-000001','ASM-2026-0001',"
                 "'2026-03-10T21:15:00Z',2150)")
    print("    LRN-2026-000001 submitted SUB-2026-000001 for ASM-2026-0001")

    step(2, "Similarity report and threshold explanation")
    score = 72.5
    band = rules.score_band(score, rule)
    conn.execute("INSERT INTO similarity_report VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (REPORT, "SUB-2026-000001", rule["integrity_rule_id"], "2026-03-11T08:00:00Z", score, 6, 0.04, 0,
                  band, 1, "SYNTHETIC://similarity-service/SUB-2026-000001/matches"))
    print(f"    score {score} under {rule['integrity_rule_id']} v{rule['version']}: "
          f"<{rule['low_threshold']} none, {rule['low_threshold']}-{rule['standard_threshold']} low, "
          f"{rule['standard_threshold']}-{rule['priority_threshold']} standard, {rule['priority_threshold']}+ priority "
          f"-> band '{band}'")

    step(3, "The flag opens a review case but does NOT mark the learner guilty")
    wf.open_case(CASE, REPORT, "2026-03-11T10:00:00Z")
    wf.add_evidence(CASE, "similarity_report", f"SYNTHETIC://similarity-report/{REPORT}", "Report attached at triage",
                    "SYSTEM-TRIAGE", "2026-03-11T10:00:00Z", REPORT)
    n = conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
    print(f"    case status = open, decisions recorded = {n}")
    expect_block("decision with no assigned human reviewer",
                 lambda: wf.record_decision(CASE, "REV-2026-0002", "confirmed_breach", GOOD_RATIONALE,
                                            "2026-03-11T11:00:00Z"))

    step(4, "Assign a reviewer, with the conflict-of-interest guard")
    expect_block("conflicted reviewer REV-2026-0001",
                 lambda: wf.assign_reviewer(CASE, "REV-2026-0001", "primary", "2026-03-11T12:00:00Z"))
    wf.assign_reviewer(CASE, "REV-2026-0002", "primary", "2026-03-11T12:00:00Z")
    print("    REV-2026-0002 assigned as primary reviewer")

    step(5, "Add evidence (timestamped, with an audit event)")
    eid = wf.add_evidence(CASE, "learner_statement", "SYNTHETIC://statements/1", "Learner explanation",
                          "LRN-2026-000001", "2026-03-13T09:30:00Z")
    print(f"    {eid} added at 2026-03-13T09:30:00Z")

    step(6, "Record a decision with a mandatory rationale")
    expect_block("decision without a real rationale",
                 lambda: wf.record_decision(CASE, "REV-2026-0002", "confirmed_breach", "copied",
                                            "2026-03-16T15:00:00Z"))
    did = wf.record_decision(CASE, "REV-2026-0002", "confirmed_breach", GOOD_RATIONALE, "2026-03-16T15:00:00Z")
    print(f"    {did}: confirmed_breach by REV-2026-0002")
    expect_block("closing an adverse case inside the appeal window",
                 lambda: wf.close_case(CASE, "2026-03-17T09:00:00Z"))

    step(7, "Submit an appeal and assign an independent reviewer")
    aid = wf.submit_appeal(CASE, "Sources were cited in an appendix that the checker ignored.", "2026-03-18T10:00:00Z")
    expect_block("original decision-maker as appeal reviewer",
                 lambda: wf.assign_appeal_reviewer(aid, "REV-2026-0002", "2026-03-18T12:00:00Z"))
    expect_block("closing while the appeal is pending", lambda: wf.close_case(CASE, "2026-03-18T13:00:00Z"))
    wf.assign_appeal_reviewer(aid, "REV-2026-0003", "2026-03-18T12:00:00Z")
    print(f"    {aid}: independent reviewer REV-2026-0003 assigned")

    step(8, "Record the appeal outcome and close the case")
    wf.record_appeal_outcome(aid, "modified", "Appendix citations cover part of the matched text; sanction reduced.",
                             "2026-03-24T16:00:00Z", "educational_warning")
    wf.close_case(CASE, "2026-03-25T09:00:00Z")
    for d in conn.execute("SELECT decision_id, outcome, supersedes_decision_id FROM decision ORDER BY decision_round"):
        print(f"    {d['decision_id']}: {d['outcome']}" + (f" (supersedes {d['supersedes_decision_id']})"
                                                           if d['supersedes_decision_id'] else " (kept in history)"))

    step(9, "Case timeline (append-only audit trail)")
    for e in conn.execute("SELECT event_seq, event_at, event_type, actor_id, entity_id FROM case_event "
                          "WHERE case_id=? ORDER BY event_seq", (CASE,)):
        print(f"    {e['event_seq']:>2}  {e['event_at']}  {e['event_type']:<28} {e['actor_id']:<18} {e['entity_id']}")
    events = [r[0] for r in conn.execute("SELECT event_type FROM case_event WHERE case_id=? ORDER BY event_seq", (CASE,))]
    print(f"    status replayed from events = {rules.derive_status(events)}")

    step(10, "Validation on the generated dataset")
    print("    run: python -m src.validation --scale full --run-all")


if __name__ == "__main__":
    main()
