"""Deterministic synthetic data generator.

Order: master data -> submissions -> similarity reports -> cases (run through the guarded
workflow) -> labelled negative scenarios -> export CSV/JSON/SQL + separate ground truth.

Usage:
    python -m src.generate_data --scale starter
    python -m src.generate_data --scale full --seed 20261022
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rules
from .common import (DATA_DIR, PRIMARY_KEYS, TABLE_ORDER, connect, create_schema, load_settings,
                     parse_iso, sha256_file, to_iso)
from .workflow import CaseWorkflow, ConflictOfInterestError

DEPARTMENTS = ["Environmental Science", "Computer Science", "Business", "Engineering"]
DEPT_CODE = {"Environmental Science": "ENV", "Computer Science": "CSC", "Business": "BUS", "Engineering": "ENG"}
PROGRAMMES = ["BSc Environmental Science", "BSc Computer Science", "BBA", "BEng Civil", "MSc Data Science"]
CONFLICT_REASONS = ["personal tutor", "family relationship", "project supervisor", "prior complaint involvement"]
ANOMALY_TYPES = [
    "automatic_guilt",
    "reviewer_conflict",
    "evidence_history_missing",
    "appeal_closed_without_independent_review",
]


class Generator:
    def __init__(self, settings: dict, scale: str, seed: int):
        self.settings = settings
        self.scale = scale
        self.seed = seed
        self.p = settings["scales"][scale]
        self.sim = settings["simulation"]
        self.as_of = settings["as_of"]
        self.window_days = settings["appeal_window_days"]
        self.conn = connect()
        create_schema(self.conn)
        self.wf = CaseWorkflow(self.conn, settings)
        self.rules = settings["integrity_rules"]
        self.workload: dict[str, int] = {}
        self.conflict_counter = 0
        self.report_truth: list[dict] = []
        self.anomalies: list[dict] = []

    def rng(self, tag: str) -> random.Random:
        # String seeds are hashed with SHA-512 by random.Random -> stable across runs and machines.
        return random.Random(f"{self.seed}:{tag}")

    # ------------------------------------------------------------- master data
    def build_master_data(self) -> None:
        r = self.rng("learners")
        self.learners = [
            (f"LRN-2026-{i:06d}", f"Synthetic Learner {i:06d}", f"learner{i:06d}@learners.example.org",
             r.choice(PROGRAMMES), r.choice([2023, 2024, 2025]), 1)
            for i in range(1, self.p["learners"] + 1)
        ]
        self.conn.executemany("INSERT INTO learner VALUES (?,?,?,?,?,?)", self.learners)

        r = self.rng("assessments")
        base = datetime(2026, 1, 12, 23, 59, tzinfo=timezone.utc)
        self.assessments = {}
        for i in range(1, self.p["assessments"] + 1):
            dept = DEPARTMENTS[(i - 1) % len(DEPARTMENTS)]
            a_type = r.choice(rules.ASSESSMENT_TYPES)
            template = 1 if a_type in ("lab_report", "code") or r.random() < 0.2 else 0
            due = to_iso(base + timedelta(days=r.randint(0, 138)))
            row = (f"ASM-2026-{i:04d}", f"{DEPT_CODE[dept]}{100 + i}", f"{a_type.replace('_', ' ').title()} {i}",
                   dept, a_type, template, due)
            self.assessments[row[0]] = dict(zip(
                ["assessment_id", "course_code", "title", "department", "assessment_type", "template_provided",
                 "due_at"], row))
            self.conn.execute("INSERT INTO assessment VALUES (?,?,?,?,?,?,?)", row)

        for rule in self.rules:
            self.conn.execute(
                "INSERT INTO integrity_rule VALUES (?,?,?,?,?,?,?,?,?)",
                (rule["integrity_rule_id"], rule["name"], rule["version"], rule["low_threshold"],
                 rule["standard_threshold"], rule["priority_threshold"], rule["effective_from"],
                 rule["effective_to"], rule["description"]))

        self.reviewers = []
        for i in range(1, self.p["reviewers"] + 1):
            officer = i % 5 == 0
            dept = "Academic Integrity Office" if officer else DEPARTMENTS[i % len(DEPARTMENTS)]
            row = (f"REV-2026-{i:04d}", f"Synthetic Reviewer {i:04d}", f"reviewer{i:04d}@staff.example.org", dept,
                   "integrity_officer" if officer else "academic_reviewer")
            self.reviewers.append(dict(zip(["reviewer_id", "full_name", "email", "department", "reviewer_role"], row)))
            self.workload[row[0]] = 0
            self.conn.execute("INSERT INTO reviewer VALUES (?,?,?,?,?)", row)

        r = self.rng("conflicts")
        pairs = set()
        while len(pairs) < self.p["random_conflicts"]:
            pairs.add((r.choice(self.reviewers)["reviewer_id"], r.choice(self.learners)[0]))
        for reviewer_id, learner_id in sorted(pairs):
            declared = to_iso(datetime(2025, 9, 1, 9, tzinfo=timezone.utc) + timedelta(days=r.randint(0, 120)))
            self.add_conflict(reviewer_id, learner_id, r.choice(CONFLICT_REASONS), declared)

    def add_conflict(self, reviewer_id: str, learner_id: str, reason: str, declared_at: str) -> None:
        if self.conn.execute("SELECT 1 FROM reviewer_conflict WHERE reviewer_id=? AND learner_id=?",
                             (reviewer_id, learner_id)).fetchone():
            return  # already declared
        self.conflict_counter += 1
        self.conn.execute("INSERT INTO reviewer_conflict VALUES (?,?,?,?,?)",
                          (f"CONF-2026-{self.conflict_counter:06d}", reviewer_id, learner_id, reason, declared_at))

    # ------------------------------------------------------ submissions/reports
    def build_submissions(self) -> None:
        r = self.rng("submissions")
        assessment_ids = sorted(self.assessments)
        seen = set()
        self.submissions = []
        for i in range(1, self.p["submissions"] + 1):
            while True:
                pair = (r.choice(self.learners)[0], r.choice(assessment_ids))
                if pair not in seen:
                    seen.add(pair)
                    break
            due = parse_iso(self.assessments[pair[1]]["due_at"])
            submitted = to_iso(due - timedelta(minutes=r.randint(10, 72 * 60)))
            row = (f"SUB-2026-{i:06d}", pair[0], pair[1], submitted, r.randint(300, 4500))
            self.submissions.append(row)
        self.conn.executemany("INSERT INTO submission VALUES (?,?,?,?,?)", self.submissions)

    def build_reports(self) -> None:
        r = self.rng("reports")
        chosen = sorted(r.sample(range(len(self.submissions)), self.p["similarity_reports"]))
        self.reports = []
        for idx in chosen:
            sub_id, learner_id, assessment_id, submitted_at, _ = self.submissions[idx]
            generated_at = to_iso(parse_iso(submitted_at) + timedelta(minutes=r.randint(30, 48 * 60)))
            rule = rules.active_rule(self.rules, generated_at)
            template_ok = self.assessments[assessment_id]["template_provided"] == 1
            u = r.random()
            if u < 0.35:
                truth = "misconduct"
            elif u < 0.60:
                truth = "legitimate_quotation"
            elif u < 0.80 and template_ok:
                truth = "shared_template"
            else:
                truth = "coincidental_match"
            if truth == "misconduct":
                score, matched, quoted, tmpl = r.triangular(35, 98, 68), r.randint(3, 12), r.uniform(0, 0.1), 0
            elif truth == "legitimate_quotation":
                score, matched, quoted, tmpl = r.triangular(22, 85, 45), r.randint(2, 8), r.uniform(0.35, 0.9), 0
            elif truth == "shared_template":
                score, matched, quoted, tmpl = r.triangular(22, 80, 45), r.randint(1, 3), r.uniform(0, 0.15), 1
            else:
                score, matched, quoted, tmpl = r.triangular(20, 60, 30), r.randint(1, 6), r.uniform(0.05, 0.3), 0
            # Assumption: the similarity service only stores a report when the score reaches the
            # active rule's lowest threshold, so every stored report is a flag.
            score = round(max(score, rule["low_threshold"]), 2)
            band = rules.score_band(score, rule)
            report = {
                "similarity_report_id": f"SIM-{sub_id}-01", "submission_id": sub_id,
                "integrity_rule_id": rule["integrity_rule_id"], "generated_at": generated_at,
                "similarity_score": score, "matched_source_count": matched,
                "quoted_material_share": round(quoted, 3), "common_template_indicator": tmpl,
                "score_band": band, "is_flagged": int(band != "none"),
                "source_evidence_ref": f"SYNTHETIC://similarity-service/{sub_id}/matches",
                "learner_id": learner_id, "assessment_id": assessment_id,
            }
            self.reports.append(report)
            self.report_truth.append({
                "similarity_report_id": report["similarity_report_id"], "submission_id": sub_id,
                "true_conduct": truth, "flag_is_false_positive": int(truth != "misconduct"),
            })
            self.conn.execute(
                "INSERT INTO similarity_report VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                tuple(report[k] for k in [
                    "similarity_report_id", "submission_id", "integrity_rule_id", "generated_at", "similarity_score",
                    "matched_source_count", "quoted_material_share", "common_template_indicator", "score_band",
                    "is_flagged", "source_evidence_ref"]))

    # ------------------------------------------------------------------- cases
    def build_cases(self) -> None:
        """Case selection (documented): rank flags by band (priority > standard > low), then score
        (high first), then report ID; the top N open a case. Uses observable data only."""
        ranked = sorted(self.reports, key=lambda x: (-rules.BAND_RANK[x["score_band"]], -x["similarity_score"],
                                                     x["similarity_report_id"]))
        selected = ranked[: self.p["cases"]]
        r = self.rng("case-open")
        for rep in selected:
            rep["opened_at"] = to_iso(parse_iso(rep["generated_at"]) + timedelta(minutes=r.randint(60, 72 * 60)))
        selected.sort(key=lambda x: (x["opened_at"], x["similarity_report_id"]))
        for i, rep in enumerate(selected, start=1):
            rep["case_id"] = f"CASE-2026-{i:06d}"

        cutoff = to_iso(parse_iso(self.as_of) - timedelta(days=60))
        eligible = [rep["case_id"] for rep in selected if rep["opened_at"] <= cutoff]
        k = self.p["anomalies_per_type"]
        picked = self.rng("anomalies").sample(eligible, k * len(ANOMALY_TYPES))
        self.anomaly_for_case = {case_id: ANOMALY_TYPES[i // k] for i, case_id in enumerate(picked)}

        for rep in selected:
            self.process_case(rep, self.anomaly_for_case.get(rep["case_id"]))

    def candidates(self, department: str, role: str) -> list[str]:
        if role == "integrity_officer":
            pool = [rv for rv in self.reviewers if rv["reviewer_role"] == "integrity_officer"]
        else:
            pool = [rv for rv in self.reviewers
                    if rv["reviewer_role"] == "academic_reviewer" and rv["department"] == department]
            if len(pool) < 3:
                pool = [rv for rv in self.reviewers if rv["reviewer_role"] == "academic_reviewer"]
        # Workload balancing: fewest cases assigned so far, tie-break on reviewer ID.
        return [rv["reviewer_id"] for rv in sorted(pool, key=lambda rv: (self.workload[rv["reviewer_id"]],
                                                                          rv["reviewer_id"]))]

    def assign_with_guard(self, case_id: str, pool: list[str], role: str, at: str, exclude: set[str]) -> str:
        for reviewer_id in pool:
            if reviewer_id in exclude:
                continue
            try:
                if role == "appeal":
                    self.wf.assign_appeal_reviewer(case_id, reviewer_id, at)  # case_id holds appeal_id here
                else:
                    self.wf.assign_reviewer(case_id, reviewer_id, role, at)
            except ConflictOfInterestError:
                continue  # blocked attempt is recorded; try the next reviewer
            self.workload[reviewer_id] += 1
            return reviewer_id
        raise RuntimeError(f"No eligible reviewer for {case_id}")

    def process_case(self, rep: dict, anomaly: str | None) -> None:
        case_id, report_id = rep["case_id"], rep["similarity_report_id"]
        r = self.rng(f"case:{case_id}")
        wf, as_of = self.wf, self.as_of

        def later(t: str, **delta) -> str:
            return to_iso(parse_iso(t) + timedelta(**delta))

        t = rep["opened_at"]
        wf.open_case(case_id, report_id, t)
        wf.add_evidence(case_id, "similarity_report", f"SYNTHETIC://similarity-report/{report_id}",
                        "Synthetic similarity report attached at triage.", "SYSTEM-TRIAGE", t, report_id)

        if anomaly == "automatic_guilt":
            self.inject_automatic_guilt(rep, later(t, minutes=5))
            return

        t = later(t, hours=r.randint(2, 48))
        if t > as_of:
            return
        department = self.assessments[rep["assessment_id"]]["department"]
        pool = self.candidates(department, "academic_reviewer")
        if anomaly == "reviewer_conflict":
            primary = self.inject_reviewer_conflict(rep, pool[0], t)
        else:
            if r.random() < self.sim["targeted_conflict_probability"]:
                # A declared conflict for the first-choice reviewer: the guard must block it.
                self.add_conflict(pool[0], rep["learner_id"], r.choice(CONFLICT_REASONS), later(t, days=-10))
            primary = self.assign_with_guard(case_id, pool, "primary", t, set())
        involved = {primary}
        if rep["score_band"] == "priority":
            involved.add(self.assign_with_guard(case_id, self.candidates(department, "academic_reviewer"),
                                                "secondary", t, involved))

        t = later(t, hours=r.randint(1, 24))
        wf.add_evidence(case_id, "matched_source_excerpt", f"SYNTHETIC://matches/{report_id}/excerpt-1",
                        f"Reviewer extracted {rep['matched_source_count']} matched passages (synthetic).",
                        primary, t, report_id)
        if r.random() < self.sim["learner_statement_probability"]:
            t = later(t, days=r.randint(1, 4))
            wf.add_evidence(case_id, "learner_statement", f"SYNTHETIC://statements/{case_id}",
                            "Learner explanation of sources and citations (synthetic).", rep["learner_id"], t)
        if r.random() < self.sim["draft_history_probability"]:
            t = later(t, hours=r.randint(1, 12))
            wf.add_evidence(case_id, "draft_history", f"SYNTHETIC://lms/{rep['submission_id']}/drafts",
                            "Draft version history from the learning platform (synthetic).", "SYSTEM-LMS", t)

        t = later(t, days=r.randint(1, 10), hours=r.randint(0, 8))
        if t > as_of:
            return
        if anomaly == "appeal_closed_without_independent_review":
            outcome = "educational_warning"  # scenario needs an appealable decision
        else:
            outcome = rules.simulate_review_outcome(rep, r)
        decision_at = t
        wf.record_decision(case_id, primary, outcome, self.rationale(rep, outcome), decision_at)

        if r.random() < self.sim["late_reviewer_note_probability"]:
            wf.add_evidence(case_id, "reviewer_note", f"SYNTHETIC://notes/{case_id}/post-decision",
                            "Post-decision reviewer note (recorded with an audit event).", primary,
                            later(decision_at, minutes=30))
        if anomaly == "evidence_history_missing":
            self.inject_evidence_without_history(rep, primary, later(decision_at, minutes=45))

        if outcome in rules.ADVERSE_OUTCOMES and (
                anomaly == "appeal_closed_without_independent_review" or r.random() < self.sim["appeal_probability"]):
            t = later(decision_at, days=r.randint(1, self.window_days - 1), hours=r.randint(0, 12))
            if t > as_of:
                return
            appeal_id = wf.submit_appeal(case_id, "Learner disputes the finding and cites properly referenced "
                                                  "material (synthetic grounds).", t)
            if anomaly == "appeal_closed_without_independent_review":
                self.inject_appeal_without_independent_review(rep, appeal_id, primary, later(t, days=3))
                return
            t = later(t, hours=r.randint(4, 72))
            if t > as_of:
                return
            self.assign_with_guard(appeal_id, self.candidates(department, "integrity_officer"), "appeal", t,
                                   involved)
            t = later(t, days=r.randint(2, 10))
            if t > as_of:
                return
            appeal_outcome, revised = rules.simulate_appeal_outcome(rep, outcome, r)
            wf.record_appeal_outcome(appeal_id, appeal_outcome,
                                     f"Independent appeal review concluded '{appeal_outcome}' after re-examining "
                                     f"quoted share {rep['quoted_material_share']} and template indicator "
                                     f"{rep['common_template_indicator']}.", t, revised)
            close_at = later(t, hours=r.randint(2, 48))
        elif outcome in rules.ADVERSE_OUTCOMES:
            close_at = later(decision_at, days=self.window_days + r.randint(1, 3))
        else:
            close_at = later(decision_at, hours=r.randint(1, 48))
        if close_at <= as_of:
            wf.close_case(case_id, close_at)

    @staticmethod
    def rationale(rep: dict, outcome: str) -> str:
        return (f"Outcome '{outcome}' after reviewing evidence: similarity {rep['similarity_score']}%, "
                f"{rep['matched_source_count']} matched sources, quoted share {rep['quoted_material_share']}, "
                f"template indicator {rep['common_template_indicator']}. Score treated as a signal, not proof.")

    # ------------------------------------------ labelled negative scenarios
    # Each scenario deliberately BYPASSES the workflow guard with raw SQL, and is recorded
    # only in the separate ground-truth file.
    def record_anomaly(self, issue_type, case_id, entity_type, entity_id, description):
        self.anomalies.append({
            "anomaly_id": f"ANM-{len(self.anomalies) + 1:04d}", "issue_type": issue_type, "case_id": case_id,
            "entity_type": entity_type, "entity_id": entity_id, "description": description})

    def inject_automatic_guilt(self, rep: dict, at: str) -> None:
        case_id = rep["case_id"]
        decision_id = f"DEC-{case_id}-01"
        self.conn.execute(
            "INSERT INTO decision VALUES (?,?,?,?,?,?,?,?,?)",
            (decision_id, case_id, None, "automated", "confirmed_breach",
             f"Automatic finding: similarity score {rep['similarity_score']} is above the priority threshold.",
             at, 1, None))
        self.wf.event(case_id, "decision_recorded", at, "SYSTEM-AUTO", "decision", decision_id,
                      {"outcome": "confirmed_breach", "decision_round": 1})
        self.conn.execute("UPDATE integrity_case SET status='decided' WHERE case_id=?", (case_id,))
        self.record_anomaly("automatic_guilt", case_id, "decision", decision_id,
                            "Flag converted straight into confirmed_breach with no human reviewer.")

    def inject_reviewer_conflict(self, rep: dict, reviewer_id: str, at: str) -> str:
        case_id = rep["case_id"]
        self.add_conflict(reviewer_id, rep["learner_id"], "project supervisor",
                          to_iso(parse_iso(at) - timedelta(days=30)))
        assignment_id = f"ASG-{case_id}-01"
        self.conn.execute("INSERT INTO case_reviewer VALUES (?,?,?,?,?,?,?,?)",
                          (assignment_id, case_id, reviewer_id, "primary", "active", at, None, None))
        self.wf.event(case_id, "reviewer_assigned", at, "SYSTEM-ASSIGNMENT", "case_reviewer", assignment_id,
                      {"reviewer_id": reviewer_id, "role": "primary"})
        self.conn.execute("UPDATE integrity_case SET status='under_review' WHERE case_id=?", (case_id,))
        self.workload[reviewer_id] += 1
        self.record_anomaly("reviewer_conflict", case_id, "case_reviewer", assignment_id,
                            "Reviewer assigned despite a declared conflict with the learner.")
        return reviewer_id

    def inject_evidence_without_history(self, rep: dict, reviewer_id: str, at: str) -> None:
        case_id = rep["case_id"]
        seq = self.conn.execute("SELECT COUNT(*) FROM evidence_item WHERE case_id=?", (case_id,)).fetchone()[0] + 1
        evidence_id = f"EVD-{case_id}-{seq:03d}"
        self.conn.execute(
            "INSERT INTO evidence_item VALUES (?,?,?,?,?,?,?,?,?)",
            (evidence_id, case_id, None, "reviewer_note", f"SYNTHETIC://notes/{case_id}/untracked",
             "Evidence added after the decision without an audit event.", reviewer_id, at, seq))
        self.record_anomaly("evidence_history_missing", case_id, "evidence_item", evidence_id,
                            "Evidence added after decision with no evidence_added event.")

    def inject_appeal_without_independent_review(self, rep: dict, appeal_id: str, original_reviewer: str,
                                                 at: str) -> None:
        case_id = rep["case_id"]
        self.conn.execute(
            "UPDATE appeal SET appeal_reviewer_id=?, outcome='upheld', outcome_rationale=?, decided_at=? "
            "WHERE appeal_id=?",
            (original_reviewer, "Appeal reviewed and closed by the original decision-maker.", at, appeal_id))
        self.wf.event(case_id, "appeal_decided", at, original_reviewer, "appeal", appeal_id, {"outcome": "upheld"})
        close_at = to_iso(parse_iso(at) + timedelta(hours=1))
        self.conn.execute("UPDATE case_reviewer SET assignment_status='completed', unassigned_at=? "
                          "WHERE case_id=? AND assignment_status='active'", (close_at, case_id))
        self.conn.execute("UPDATE integrity_case SET status='closed', closed_at=? WHERE case_id=?",
                          (close_at, case_id))
        self.wf.event(case_id, "case_closed", close_at, "SYSTEM-CASE-ADMIN", "appeal", appeal_id,
                      {"final_outcome": "educational_warning"})
        self.record_anomaly("appeal_closed_without_independent_review", case_id, "appeal", appeal_id,
                            "Appeal decided by the original decision-maker and the case closed.")

    # ------------------------------------------------------------------- run
    def run(self) -> sqlite3.Connection:
        self.build_master_data()
        self.build_submissions()
        self.build_reports()
        self.build_cases()
        self.conn.commit()
        return self.conn


# ---------------------------------------------------------------- exports
def _fmt(value) -> str:
    return "" if value is None else str(value)


def export_dataset(conn: sqlite3.Connection, gen: Generator, out_root: Path) -> dict:
    gen_dir = out_root / "generated" / gen.scale
    gt_dir = out_root / "ground_truth" / gen.scale
    for sub in ("csv", "json"):
        (gen_dir / sub).mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    counts = {}
    for table in TABLE_ORDER:
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY {PRIMARY_KEYS[table]}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        counts[table] = len(rows)
        with open(gen_dir / "csv" / f"{table}.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(cols)
            writer.writerows([[_fmt(v) for v in row] for row in rows])
        with open(gen_dir / "json" / f"{table}.json", "w", encoding="utf-8") as fh:
            json.dump([dict(zip(cols, row)) for row in rows], fh, indent=1)
            fh.write("\n")

    # SQL seed script for reference/master tables.
    with open(gen_dir / "seed_reference_data.sql", "w", encoding="utf-8") as fh:
        fh.write("-- Generated reference data (synthetic). Load after sql/schema.sql.\n")
        for table in ("integrity_rule", "assessment", "reviewer"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {PRIMARY_KEYS[table]}"):
                values = ", ".join("NULL" if v is None else (str(v) if isinstance(v, (int, float))
                                   else "'" + str(v).replace("'", "''") + "'") for v in row)
                fh.write(f"INSERT INTO {table} VALUES ({values});\n")

    _write_csv(gt_dir / "anomalies.csv", gen.anomalies,
               ["anomaly_id", "issue_type", "case_id", "entity_type", "entity_id", "description"])
    _write_csv(gt_dir / "report_truth.csv", gen.report_truth,
               ["similarity_report_id", "submission_id", "true_conduct", "flag_is_false_positive"])

    files = sorted(p for p in list(gen_dir.rglob("*")) + list(gt_dir.rglob("*"))
                   if p.is_file() and p.name != "manifest.json")
    manifest = {
        "dataset": "Academic integrity similarity case management (synthetic)",
        "scale": gen.scale,
        "seed": gen.seed,
        "parameters": gen.p,
        "as_of": gen.as_of,
        "row_counts": counts,
        "ground_truth_rows": {"anomalies": len(gen.anomalies), "report_truth": len(gen.report_truth)},
        "data_sources": "All records are synthetic, generated by src/generate_data.py. No external or personal "
                        "data was used. Names use the 'Synthetic Learner/Reviewer' pattern and example.org emails; "
                        "evidence references use the SYNTHETIC:// scheme and point to nothing real.",
        "files": {p.relative_to(out_root).as_posix(): sha256_file(p) for p in files},
    }
    with open(gen_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return manifest


def _write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate(scale: str, seed: int | None = None, out_root: Path = DATA_DIR, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    seed = settings["seed"] if seed is None else seed
    gen = Generator(settings, scale, seed)
    conn = gen.run()
    return export_dataset(conn, gen, Path(out_root))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", choices=["starter", "full"], default="starter")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    manifest = generate(args.scale, args.seed, args.out_dir)
    print(f"Generated scale={manifest['scale']} seed={manifest['seed']} into {args.out_dir}")
    for table, n in manifest["row_counts"].items():
        print(f"  {table:<20} {n:>6}")
    print(f"  ground truth anomalies: {manifest['ground_truth_rows']['anomalies']}")


if __name__ == "__main__":
    main()
