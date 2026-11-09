# Entity relationship diagram

The 10 required entities, plus three supporting tables: `reviewer_conflict` (declared conflicts),
`case_reviewer` (the Case N:M Reviewer bridge, with assignment history) and `case_event` (the audit
timeline).

```mermaid
erDiagram
    LEARNER ||--o{ SUBMISSION : makes
    ASSESSMENT ||--o{ SUBMISSION : receives
    SUBMISSION ||--o| SIMILARITY_REPORT : "checked by (0..1)"
    INTEGRITY_RULE ||--o{ SIMILARITY_REPORT : "in force for"
    SIMILARITY_REPORT ||--o| INTEGRITY_CASE : "may open (0..1)"
    SUBMISSION ||--o{ INTEGRITY_CASE : concerns
    INTEGRITY_RULE ||--o{ INTEGRITY_CASE : "applied to"
    INTEGRITY_CASE ||--o{ CASE_REVIEWER : has
    REVIEWER ||--o{ CASE_REVIEWER : "assigned via"
    REVIEWER ||--o{ REVIEWER_CONFLICT : declares
    LEARNER ||--o{ REVIEWER_CONFLICT : "subject of"
    INTEGRITY_CASE ||--o{ EVIDENCE_ITEM : holds
    SIMILARITY_REPORT ||--o{ EVIDENCE_ITEM : "referenced by"
    INTEGRITY_CASE ||--o{ DECISION : "has (rounds)"
    REVIEWER ||--o{ DECISION : records
    DECISION ||--o| DECISION : supersedes
    INTEGRITY_CASE ||--o{ APPEAL : has
    DECISION ||--o| APPEAL : "appealed by (0..1)"
    REVIEWER ||--o{ APPEAL : "independently reviews"
    INTEGRITY_CASE ||--o{ CASE_EVENT : "timeline of"
```

## Cardinality notes

| Relationship | Cardinality | Notes |
|---|---|---|
| Learner – Submission | 1 : N | One submission per learner per assessment (`UNIQUE(learner_id, assessment_id)`). |
| Assessment – Submission | 1 : N | |
| Submission – SimilarityReport | 1 : 0..1 | Enforced by `UNIQUE(submission_id)` (assumption A1). |
| IntegrityRule – SimilarityReport | 1 : N | The rule whose effective window contains `generated_at`. |
| SimilarityReport – Case | 1 : 0..1 | Only flagged reports can open a case; `UNIQUE(similarity_report_id)`. |
| Case – Reviewer | N : M | Through `case_reviewer` (role primary / secondary / appeal; status active / completed / blocked_conflict). |
| Case – EvidenceItem | 1 : N | Append-only, `case_evidence_seq` 1..n. |
| Case – Decision | 1 : N | Round 1 = first decision; a successful appeal adds a round that supersedes the earlier one. |
| Decision – Appeal | 1 : 0..1 | `UNIQUE(decision_id)`: one appeal per decision. Case – Appeal is 1 : N across rounds. |
| Case – CaseEvent | 1 : N | Every state change; `event_seq` 1..n per case. |

Optional relationships: `decision.reviewer_id` is nullable only so the *automatic guilt* negative
scenario can be represented and then detected; the workflow never writes a NULL there.
`appeal.appeal_reviewer_id` stays NULL until an independent reviewer is assigned.

## Lifecycle

```
open --reviewer_assigned--> under_review --decision_recorded--> decided --case_closed--> closed
                                                                   |  ^
                                                    appeal_submitted  appeal_decided
                                                                   v  |
                                                                appeal_open
```
