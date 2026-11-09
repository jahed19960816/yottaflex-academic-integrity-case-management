# Data dictionary

Conventions: all timestamps are ISO 8601 UTC text `YYYY-MM-DDTHH:MM:SSZ`. **Req** = required (NOT
NULL). **Source** = a recorded fact; **Derived** = calculated from other records. IDs are
immutable and identical in every table, CSV, JSON and SQL file.

## learner
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| learner_id | TEXT PK | yes | `LRN-2026-NNNNNN`, unique | Source |
| full_name | TEXT | yes | `Synthetic Learner NNNNNN` | Source |
| email | TEXT | yes | unique, `@learners.example.org` | Source |
| programme | TEXT | yes | programme name | Source |
| cohort_year | INTEGER | yes | 2023–2025 | Source |
| is_synthetic | INTEGER | yes | must be 1 | Source |

## assessment
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| assessment_id | TEXT PK | yes | `ASM-2026-NNNN` | Source |
| course_code | TEXT | yes | e.g. `ENV101` | Source |
| title | TEXT | yes | | Source |
| department | TEXT | yes | Environmental Science, Computer Science, Business, Engineering | Source |
| assessment_type | TEXT | yes | essay, report, lab_report, code | Source |
| template_provided | INTEGER | yes | 0/1; 1 means a shared template legitimately produces matching text | Source |
| due_at | TEXT | yes | timestamp | Source |

## integrity_rule
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| integrity_rule_id | TEXT PK | yes | `INT-2026-NNNNNN` | Source |
| name, description | TEXT | yes | | Source |
| version | INTEGER | yes | 1, 2, … | Source |
| low_threshold / standard_threshold / priority_threshold | REAL | yes | percent; 0 ≤ low < standard < priority ≤ 100 | Source |
| effective_from | TEXT | yes | timestamp, inclusive | Source |
| effective_to | TEXT | no | timestamp, exclusive; NULL = still in force | Source |

## reviewer
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| reviewer_id | TEXT PK | yes | `REV-2026-NNNN` | Source |
| full_name | TEXT | yes | `Synthetic Reviewer NNNN` | Source |
| email | TEXT | yes | unique, `@staff.example.org` | Source |
| department | TEXT | yes | academic department or Academic Integrity Office | Source |
| reviewer_role | TEXT | yes | academic_reviewer, integrity_officer | Source |

## reviewer_conflict (supporting)
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| conflict_id | TEXT PK | yes | `CONF-2026-NNNNNN` | Source |
| reviewer_id | TEXT FK → reviewer | yes | | Source |
| learner_id | TEXT FK → learner | yes | unique together with reviewer_id | Source |
| reason | TEXT | yes | personal tutor, family relationship, project supervisor, prior complaint involvement | Source |
| declared_at | TEXT | yes | the conflict applies to assignments at or after this time | Source |

## submission
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| submission_id | TEXT PK | yes | `SUB-2026-NNNNNN`, sequential | Source |
| learner_id | TEXT FK → learner | yes | | Source |
| assessment_id | TEXT FK → assessment | yes | unique with learner_id | Source |
| submitted_at | TEXT | yes | before the assessment due date | Source |
| word_count | INTEGER | yes | > 0 | Source |

## similarity_report
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| similarity_report_id | TEXT PK | yes | `SIM-<submission_id>-01` | Source |
| submission_id | TEXT FK → submission | yes | unique | Source |
| integrity_rule_id | TEXT FK → integrity_rule | yes | rule in force at generated_at | Derived |
| generated_at | TEXT | yes | after submitted_at | Source |
| similarity_score | REAL | yes | 0–100 (percent) | Source |
| matched_source_count | INTEGER | yes | ≥ 0 | Source |
| quoted_material_share | REAL | yes | 0–1, share of matched text inside quotation marks | Source |
| common_template_indicator | INTEGER | yes | 0/1 | Source |
| score_band | TEXT | yes | none, low, standard, priority — from score and rule thresholds | Derived |
| is_flagged | INTEGER | yes | 1 if band ≠ none | Derived |
| source_evidence_ref | TEXT | yes | `SYNTHETIC://…` | Source |

## integrity_case
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| case_id | TEXT PK | yes | `CASE-2026-NNNNNN`, in order of opened_at | Source |
| submission_id | TEXT FK → submission | yes | same as the report's submission | Source |
| similarity_report_id | TEXT FK → similarity_report | yes | unique; report must be flagged | Source |
| integrity_rule_id | TEXT FK → integrity_rule | yes | copied from the report | Derived |
| review_priority | TEXT | yes | low, standard, priority (= report band) | Derived |
| opened_at | TEXT | yes | after the report | Source |
| status | TEXT | yes | open, under_review, decided, appeal_open, closed — replayed from case_event | Derived |
| closed_at | TEXT | no | set only when status = closed | Source |

## case_reviewer (bridge Case N:M Reviewer)
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| case_reviewer_id | TEXT PK | yes | `ASG-<case_id>-NN` | Source |
| case_id | TEXT FK → integrity_case | yes | | Source |
| reviewer_id | TEXT FK → reviewer | yes | | Source |
| assignment_role | TEXT | yes | primary, secondary, appeal | Source |
| assignment_status | TEXT | yes | active, completed, blocked_conflict | Source |
| assigned_at | TEXT | yes | at or after opened_at | Source |
| unassigned_at | TEXT | no | set when completed or blocked | Source |
| status_reason | TEXT | no | conflict ID and reason when blocked | Source |

## evidence_item
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| evidence_id | TEXT PK | yes | `EVD-<case_id>-NNN` | Source |
| case_id | TEXT FK → integrity_case | yes | | Source |
| similarity_report_id | TEXT FK → similarity_report | no | when the evidence comes from the report | Source |
| evidence_type | TEXT | yes | similarity_report, matched_source_excerpt, learner_statement, draft_history, reviewer_note | Source |
| evidence_ref | TEXT | yes | `SYNTHETIC://…` | Source |
| description | TEXT | yes | | Source |
| added_by | TEXT | yes | reviewer ID, the case's learner ID, or `SYSTEM-*` | Source |
| added_at | TEXT | yes | at or after opened_at | Source |
| case_evidence_seq | INTEGER | yes | 1..n per case, unique with case_id | Source |

## decision
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| decision_id | TEXT PK | yes | `DEC-<case_id>-NN` (NN = round) | Source |
| case_id | TEXT FK → integrity_case | yes | | Source |
| reviewer_id | TEXT FK → reviewer | no* | *the workflow always sets it; NULL only in the labelled automatic-guilt scenario | Source |
| decision_source | TEXT | yes | human_review (workflow) or automated (negative scenario only) | Source |
| outcome | TEXT | yes | no_breach, insufficient_evidence, educational_warning, confirmed_breach, referred_for_further_review | Source |
| rationale | TEXT | yes | at least 30 characters | Source |
| decided_at | TEXT | yes | after opened_at and after evidence exists | Source |
| decision_round | INTEGER | yes | 1..n, unique with case_id | Source |
| supersedes_decision_id | TEXT FK → decision | no | set when an appeal revised the decision | Source |

## appeal
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| appeal_id | TEXT PK | yes | `APL-<case_id>-NN` | Source |
| case_id | TEXT FK → integrity_case | yes | | Source |
| decision_id | TEXT FK → decision | yes | unique; must be adverse | Source |
| submitted_at | TEXT | yes | within 14 days after decided_at | Source |
| grounds | TEXT | yes | | Source |
| appeal_reviewer_id | TEXT FK → reviewer | no | independent reviewer; NULL until assigned | Source |
| reviewer_assigned_at | TEXT | no | | Source |
| outcome | TEXT | yes | upheld, overturned, modified, rejected_out_of_scope, pending | Source |
| outcome_rationale | TEXT | no | required once decided | Source |
| decided_at | TEXT | no | set once decided | Source |

Appeal success (derived) = outcome in (overturned, modified).

## case_event (supporting audit trail)
| Field | Type | Req | Allowed values / rule | Kind |
|---|---|---|---|---|
| event_id | TEXT PK | yes | `EVT-<case_id>-NNNN` | Source |
| case_id | TEXT FK → integrity_case | yes | | Source |
| event_seq | INTEGER | yes | 1..n per case, time never decreases | Source |
| event_type | TEXT | yes | case_opened, reviewer_assigned, reviewer_assignment_blocked, evidence_added, decision_recorded, appeal_submitted, appeal_reviewer_assigned, appeal_decided, case_closed | Source |
| event_at | TEXT | yes | | Source |
| actor_id | TEXT | yes | reviewer, learner or `SYSTEM-*` | Source |
| entity_type / entity_id | TEXT | no | the record the event concerns | Source |
| details | TEXT | no | JSON with extra context | Source |

## Ground truth (separate, never loaded into the database)
| File | Fields |
|---|---|
| `data/ground_truth/<scale>/report_truth.csv` | similarity_report_id, submission_id, true_conduct (misconduct, legitimate_quotation, shared_template, coincidental_match), flag_is_false_positive |
| `data/ground_truth/<scale>/anomalies.csv` | anomaly_id, issue_type, case_id, entity_type, entity_id, description |

## Derived metrics
See `outputs/validation_report_full.md`: flag rate, case creation rate, false-positive rate,
decision turnaround, appeal rate, appeal success rate, independent review compliance, conflict
violations, audit completeness, reviewer workload.
