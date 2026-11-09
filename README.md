# Academic Integrity Similarity Case Management

An end-to-end, auditable workflow for academic-integrity similarity flags. It covers synthetic
data, a relational schema, guarded business rules, labelled negative scenarios, validation and
reproducibility checks.

**Core principle:** a high similarity score is a *review signal, not proof*. A flag can only open a
case for human review. An outcome needs an assigned, non-conflicted human reviewer, evidence and a
written rationale. Appeals need an independent reviewer before the case can close. Every state
change is recorded in an append-only timeline.

All data is synthetic. No personal or external data is used.

---

## Quick start

Requires **Python 3.11+**. The pipeline uses only the standard library. `pytest` is needed only
for the tests.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# 1. Generate data (fixed seed 20261022 from config/settings.json)
python -m src.generate_data --scale starter      # 1,000 submissions / 100 flags / 30 cases
python -m src.generate_data --scale full         # 10,000 submissions / 1,000 flags / 300 cases

# 2. Build the SQLite database from the exported CSV files
python -m src.database --init --scale full

# 3. Validate: keys, enums, dates, lifecycle, reconciliation, metrics,
#    negative-scenario detection vs ground truth, and double-generation checksums
python -m src.validation --scale full --run-all

# 4. Walk through one case end to end and watch each guard block a bad action
python -m src.demo

# 5. Analysis queries (funnel, outcomes by band, appeals, workload, conflicts, turnaround)
python -m src.analysis --scale full

# 6. Automated tests
python -m pytest -q
```

The generated data is already committed in `data/`, so steps 2–6 also work without step 1.
Step 1 overwrites it with byte-identical files.

### Expected output (validation, full scale)

```
Operational checks: 101/101 passed
  automatic_guilt                            expected 2  detected 2  ok
  reviewer_conflict                          expected 2  detected 2  ok
  evidence_history_missing                   expected 2  detected 2  ok
  appeal_closed_without_independent_review   expected 2  detected 2  ok
Reproducibility: same seed identical=True, different seed differs=True, committed data matches=True
Audit completeness: 99.85%
OVERALL: PASS
```

`pytest` prints `23 passed`. Audit completeness is below 100% only because of the planted
evidence-history and appeal scenarios. Each one is accounted for in the evaluation table.

---

## Results (full scale, seed 20261022)

Full report: [`outputs/validation_report_full.md`](outputs/validation_report_full.md). The
machine-readable version is in `outputs/validation_summary_full.json`.

| Metric | Value |
|---|---|
| Submissions → flags → cases | 10,000 → 1,000 (10.0% flag rate) → 300 (30.0% of flags) |
| False-positive share of reviewed flags (ground truth) | 25.1% of 291 reviewed cases |
| Decision turnaround | mean 8.6 days, p90 12.8 days |
| Appeal rate / appeal success rate | 30.1% / 25.0% |
| Independent review compliance | 97.2% (the misses are exactly the 2 planted scenarios) |
| Conflict violations / conflicted attempts blocked by the guard | 2 (planted) / 36 |
| Operational checks | 101 / 101 pass |
| Negative scenarios detected | 8 / 8, no false alarms |
| Same seed twice | identical SHA-256 for all 29 data files |

---

## How the requirements are met

| Requirement | Where |
|---|---|
| 10 entities with keys, FKs, cardinalities | `sql/schema.sql`, `docs/erd.md`, `docs/data_dictionary.md` |
| 10,000 submissions, 1,000 flags, 300 cases | `config/settings.json` → `src/generate_data.py` (checked by `row_count_*`) |
| Legitimate quotes, shared templates, false positives | report features in `src/generate_data.py`; outcome analysis in `src/analysis.py` |
| Thresholds trigger review, not guilt | `CaseWorkflow.open_case` creates no decision; `record_decision` needs an assigned human reviewer |
| Conflict-of-interest check | `CaseWorkflow.assign_reviewer` / `assign_appeal_reviewer` block the assignment and log it |
| Decisions with rationale and timestamps | `CaseWorkflow.record_decision` (rationale of at least 30 characters) |
| Appeals need independent review before closure | `assign_appeal_reviewer`, `record_appeal_outcome`, `close_case` |
| Auditable, non-overwritten history | `case_event`, append-only evidence, superseding decisions |
| Four labelled negative scenarios | `Generator.inject_*` → `data/ground_truth/<scale>/anomalies.csv` |
| Ground truth kept separate | `data/ground_truth/`, read only by the evaluation step; tested |
| PK/FK, enum, date-order, lifecycle, duplicate, reconciliation checks | `src/validation.py` |
| Fixed-seed reproducibility with checksums | `src/validation.py::reproducibility`, `tests/test_pipeline.py` |
| CSV/JSON/SQL exports, source manifest | `data/generated/<scale>/{csv,json}/`, `seed_reference_data.sql`, `manifest.json` |
| Assumptions, decisions, limitations | `docs/` |
| AI use and prompt history | [`docs/ai_usage_and_prompt_history.md`](docs/ai_usage_and_prompt_history.md) |

## Project structure

```
config/settings.json        seed, scales, thresholds (versioned rules), appeal window, snapshot date
sql/schema.sql              tables, keys, constraints
sql/analysis_queries.sql    reporting queries
src/rules.py                enums, threshold bands, status derivation, simulated reviewer judgement
src/workflow.py             guarded state changes and the audit trail
src/generate_data.py        deterministic generator, negative scenarios, exports, manifest
src/database.py             builds SQLite from the CSV exports
src/validation.py           checks, reconciliation, metrics, ground-truth evaluation, reproducibility
src/demo.py                 end-to-end walkthrough of one case
src/analysis.py             runs the analysis queries
tests/                      workflow guards, rules, full pipeline
data/generated/<scale>/     csv/, json/, seed_reference_data.sql, manifest.json (checksums)
data/ground_truth/<scale>/  anomalies.csv, report_truth.csv (evaluation only)
outputs/                    validation_summary_<scale>.json, validation_report_<scale>.md
docs/                       assumptions, data dictionary, ERD, limitations, AI usage
```

## Configuration

Everything that is policy rather than code lives in `config/settings.json`: seed, scale sizes,
integrity-rule versions and thresholds, appeal window (14 days), minimum rationale length, snapshot
date and simulation probabilities. Pass `--seed` to generate a different dataset.
No secrets or credentials are needed.
