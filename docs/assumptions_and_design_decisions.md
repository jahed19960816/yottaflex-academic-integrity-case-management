# Assumptions and design decisions

## The core principle

A similarity score is a **review signal, not proof of misconduct**. In this system a flag can only
*open a case for human review*. Only an assigned human reviewer, after evidence exists, can record an
outcome — and that outcome needs a written rationale. The workflow code enforces this; it is not a
convention.

## Assumptions (where the brief left a choice open)

| # | Assumption | Why |
|---|---|---|
| A1 | One similarity report per submission (`SIM-<submission>-01`, `UNIQUE(submission_id)`). | Simplest model that meets the brief; the ID format leaves room for re-runs (`-02`). |
| A2 | The similarity service stores a report only when the score reaches the active rule's lowest threshold, so every stored report is a flag. 1,000 reports = 1,000 flags for 10,000 submissions (10% flag rate). | The brief asks for "1,000 similarity flags/reports". |
| A3 | Thresholds are configurable and versioned. v1 (Sep 2025 – Feb 2026): 25/45/65. v2 (from 1 Mar 2026): 20/40/60, the example values in the brief. `effective_to` is exclusive. | Shows that the rule in force *at report time* decides the band. Real thresholds need institutional policy. |
| A4 | Case selection: rank flags by band (priority > standard > low), then score, then report ID; the top N (300) open a case. The rest stay as triaged flags. | Deterministic, explainable, uses only observable data. Side effect: at full scale all 300 cases are priority band (311 priority flags exist). |
| A5 | Review outcomes: `no_breach`, `insufficient_evidence`, `educational_warning`, `confirmed_breach`, `referred_for_further_review`. Only `educational_warning` and `confirmed_breach` are adverse and can be appealed. | A learner has nothing to appeal against a non-adverse outcome. |
| A6 | Appeal outcome describes the original decision: `upheld` = it stands; `overturned` = replaced by `no_breach`; `modified` = replaced by a lesser outcome; `rejected_out_of_scope` = not admissible. Success = overturned or modified. | Avoids the "appeal upheld" vs "decision upheld" ambiguity. |
| A7 | Appeal window = 14 days after the decision. An adverse case cannot close until the window has passed or the appeal is decided. | Due process; configurable in `config/settings.json`. |
| A8 | Conflicts of interest are declared reviewer-learner pairs (`reviewer_conflict`), checked at assignment time. | Gives the guard something concrete and auditable to check. |
| A9 | An appeal reviewer is independent if they made no decision on the case, hold no primary/secondary assignment on it, and have no conflict with the learner. Appeal reviewers are integrity officers. | Matches "not the original decision-maker and has no conflict". |
| A10 | Snapshot date `as_of` = 2026-05-25. Nothing is dated after it; cases opened near it are still in progress. | Realistic mix of statuses (open appeals, cases under review). |
| A11 | Reviewer decisions are *simulated* from observable report features (score discounted for quoted material and templates, matched source count) plus small noise. They never read ground truth. | Ground truth must not drive the operational workflow. |
| A12 | All people are synthetic ("Synthetic Learner 000123", example.org emails). Evidence references use `SYNTHETIC://` and point to nothing real. | No personal data; no fabricated evidence that could pass as real. |

## Design decisions and trade-offs

**Python standard library + SQLite.** No external services, nothing to install except `pytest` for
tests. Foreign keys and CHECK constraints are enforced by the database itself. Trade-off: SQLite has
no row-level security or concurrency; fine for a reproducible demo.

**One guarded workflow class (`src/workflow.py`).** Every state change — open, assign, add evidence,
decide, appeal, assign appeal reviewer, decide appeal, close — goes through a method that checks the
rules, writes the change, and appends a `case_event`. The generator uses the same class, so the
baseline data is produced by the real rules rather than by a separate imitation of them.

**Append-only history.**
- `case_event` holds the full timeline (`event_seq` 1..n per case, time never goes backwards).
- Evidence is never edited; each item gets a per-case sequence number.
- A successful appeal does not overwrite the original decision. It adds a new decision row with
  `supersedes_decision_id`, so the original outcome stays visible.
- Blocked conflict-of-interest attempts are kept as `blocked_conflict` assignment rows plus a
  `reviewer_assignment_blocked` event.

**Status is derived, not assigned.** The stored `integrity_case.status` is updated by the workflow,
and validation replays the event history to confirm they agree for every case.

**Negative scenarios bypass the guard on purpose.** The four required scenarios are injected with
raw SQL (because the workflow would refuse them), in separate generator methods, and are listed only
in `data/ground_truth/<scale>/anomalies.csv`. Validation has one detector per scenario. The
evaluation step compares detections with ground truth: every planted case must be found and
nothing else may be flagged.

| Scenario | How it is planted | How it is detected |
|---|---|---|
| automatic_guilt | `automated` decision, no reviewer, `confirmed_breach` minutes after the case opened | decision not by a human reviewer with an assignment that predates it |
| reviewer_conflict | conflict declared, then an `active` primary assignment inserted anyway | assignment joined to a conflict declared before it |
| evidence_history_missing | evidence row inserted after the decision with no `evidence_added` event | evidence without a matching event |
| appeal_closed_without_independent_review | appeal decided by the original decision-maker, no `appeal_reviewer_assigned` event, case closed | decided appeal whose reviewer is missing, was involved, or has no assignment event |

**Determinism.** Every random choice uses `random.Random(f"{seed}:{tag}")` (string seeds are hashed
with SHA-512, so they are stable across machines). Timestamps are computed, never taken from the
clock. Exports are sorted by primary key. Generating twice with the same seed gives byte-identical
files (SHA-256 checked); a different seed changes them.

**Ground truth kept apart.** `report_truth.csv` (true conduct per flag) and `anomalies.csv` live in
`data/ground_truth/`. They are read only by the evaluation step of `src/validation.py`, never by the
generator's decision logic or the workflow. A test checks that no label column appears in the
operational CSVs.

**Scales.** `starter` (1,000 submissions / 100 flags / 30 cases) for quick runs and tests; `full`
(10,000 / 1,000 / 300) for the requested volume.
