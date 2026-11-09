-- Academic Integrity / Similarity Case Management schema (SQLite).
-- All timestamps are ISO 8601 UTC strings: YYYY-MM-DDTHH:MM:SSZ (sortable as text).
-- History is append-only: evidence, decisions, assignments and events are never overwritten.
PRAGMA foreign_keys = ON;

CREATE TABLE learner (
    learner_id      TEXT PRIMARY KEY,               -- LRN-2026-000001
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    programme       TEXT NOT NULL,
    cohort_year     INTEGER NOT NULL,
    is_synthetic    INTEGER NOT NULL DEFAULT 1 CHECK (is_synthetic = 1)
);

CREATE TABLE assessment (
    assessment_id     TEXT PRIMARY KEY,             -- ASM-2026-0001
    course_code       TEXT NOT NULL,
    title             TEXT NOT NULL,
    department        TEXT NOT NULL,
    assessment_type   TEXT NOT NULL CHECK (assessment_type IN ('essay','report','lab_report','code')),
    template_provided INTEGER NOT NULL CHECK (template_provided IN (0,1)),
    due_at            TEXT NOT NULL
);

CREATE TABLE integrity_rule (
    integrity_rule_id  TEXT PRIMARY KEY,            -- INT-2026-000001
    name               TEXT NOT NULL,
    version            INTEGER NOT NULL,
    low_threshold      REAL NOT NULL,
    standard_threshold REAL NOT NULL,
    priority_threshold REAL NOT NULL,
    effective_from     TEXT NOT NULL,
    effective_to       TEXT,                        -- exclusive; NULL = still in force
    description        TEXT NOT NULL,
    CHECK (0 <= low_threshold AND low_threshold < standard_threshold
           AND standard_threshold < priority_threshold AND priority_threshold <= 100)
);

CREATE TABLE reviewer (
    reviewer_id    TEXT PRIMARY KEY,                -- REV-2026-0001
    full_name      TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    department     TEXT NOT NULL,
    reviewer_role  TEXT NOT NULL CHECK (reviewer_role IN ('academic_reviewer','integrity_officer'))
);

-- Supporting entity: declared conflicts of interest (reviewer <-> learner).
CREATE TABLE reviewer_conflict (
    conflict_id  TEXT PRIMARY KEY,                  -- CONF-2026-000001
    reviewer_id  TEXT NOT NULL REFERENCES reviewer(reviewer_id),
    learner_id   TEXT NOT NULL REFERENCES learner(learner_id),
    reason       TEXT NOT NULL,
    declared_at  TEXT NOT NULL,
    UNIQUE (reviewer_id, learner_id)
);

CREATE TABLE submission (
    submission_id  TEXT PRIMARY KEY,                -- SUB-2026-000001
    learner_id     TEXT NOT NULL REFERENCES learner(learner_id),
    assessment_id  TEXT NOT NULL REFERENCES assessment(assessment_id),
    submitted_at   TEXT NOT NULL,
    word_count     INTEGER NOT NULL CHECK (word_count > 0),
    UNIQUE (learner_id, assessment_id)
);

CREATE TABLE similarity_report (
    similarity_report_id      TEXT PRIMARY KEY,     -- SIM-SUB-2026-000001-01
    submission_id             TEXT NOT NULL UNIQUE REFERENCES submission(submission_id),
    integrity_rule_id         TEXT NOT NULL REFERENCES integrity_rule(integrity_rule_id),
    generated_at              TEXT NOT NULL,
    similarity_score          REAL NOT NULL CHECK (similarity_score BETWEEN 0 AND 100),
    matched_source_count      INTEGER NOT NULL CHECK (matched_source_count >= 0),
    quoted_material_share     REAL NOT NULL CHECK (quoted_material_share BETWEEN 0 AND 1),
    common_template_indicator INTEGER NOT NULL CHECK (common_template_indicator IN (0,1)),
    score_band                TEXT NOT NULL CHECK (score_band IN ('none','low','standard','priority')),
    is_flagged                INTEGER NOT NULL CHECK (is_flagged IN (0,1)),
    source_evidence_ref       TEXT NOT NULL
);

CREATE TABLE integrity_case (
    case_id               TEXT PRIMARY KEY,         -- CASE-2026-000001
    submission_id         TEXT NOT NULL REFERENCES submission(submission_id),
    similarity_report_id  TEXT NOT NULL UNIQUE REFERENCES similarity_report(similarity_report_id),
    integrity_rule_id     TEXT NOT NULL REFERENCES integrity_rule(integrity_rule_id),
    review_priority       TEXT NOT NULL CHECK (review_priority IN ('low','standard','priority')),
    opened_at             TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('open','under_review','decided','appeal_open','closed')),
    closed_at             TEXT
);

-- Bridge table Case N:M Reviewer, with assignment history (blocked attempts are kept too).
CREATE TABLE case_reviewer (
    case_reviewer_id   TEXT PRIMARY KEY,            -- ASG-CASE-2026-000001-01
    case_id            TEXT NOT NULL REFERENCES integrity_case(case_id),
    reviewer_id        TEXT NOT NULL REFERENCES reviewer(reviewer_id),
    assignment_role    TEXT NOT NULL CHECK (assignment_role IN ('primary','secondary','appeal')),
    assignment_status  TEXT NOT NULL CHECK (assignment_status IN ('active','completed','blocked_conflict')),
    assigned_at        TEXT NOT NULL,
    unassigned_at      TEXT,
    status_reason      TEXT
);

CREATE TABLE evidence_item (
    evidence_id           TEXT PRIMARY KEY,         -- EVD-CASE-2026-000001-001
    case_id               TEXT NOT NULL REFERENCES integrity_case(case_id),
    similarity_report_id  TEXT REFERENCES similarity_report(similarity_report_id),
    evidence_type         TEXT NOT NULL CHECK (evidence_type IN
                              ('similarity_report','matched_source_excerpt','learner_statement','draft_history','reviewer_note')),
    evidence_ref          TEXT NOT NULL,
    description           TEXT NOT NULL,
    added_by              TEXT NOT NULL,
    added_at              TEXT NOT NULL,
    case_evidence_seq     INTEGER NOT NULL,
    UNIQUE (case_id, case_evidence_seq)
);

CREATE TABLE decision (
    decision_id            TEXT PRIMARY KEY,        -- DEC-CASE-2026-000001-01
    case_id                TEXT NOT NULL REFERENCES integrity_case(case_id),
    reviewer_id            TEXT REFERENCES reviewer(reviewer_id),
    decision_source        TEXT NOT NULL CHECK (decision_source IN ('human_review','automated')),
    outcome                TEXT NOT NULL CHECK (outcome IN
                               ('no_breach','insufficient_evidence','educational_warning','confirmed_breach','referred_for_further_review')),
    rationale              TEXT NOT NULL,
    decided_at             TEXT NOT NULL,
    decision_round         INTEGER NOT NULL,
    supersedes_decision_id TEXT REFERENCES decision(decision_id),
    UNIQUE (case_id, decision_round)
);

CREATE TABLE appeal (
    appeal_id             TEXT PRIMARY KEY,         -- APL-CASE-2026-000001-01
    case_id               TEXT NOT NULL REFERENCES integrity_case(case_id),
    decision_id           TEXT NOT NULL UNIQUE REFERENCES decision(decision_id),
    submitted_at          TEXT NOT NULL,
    grounds               TEXT NOT NULL,
    appeal_reviewer_id    TEXT REFERENCES reviewer(reviewer_id),
    reviewer_assigned_at  TEXT,
    outcome               TEXT NOT NULL CHECK (outcome IN ('upheld','overturned','modified','rejected_out_of_scope','pending')),
    outcome_rationale     TEXT,
    decided_at            TEXT
);

-- Supporting entity: full, append-only case timeline (audit trail).
CREATE TABLE case_event (
    event_id     TEXT PRIMARY KEY,                  -- EVT-CASE-2026-000001-0001
    case_id      TEXT NOT NULL REFERENCES integrity_case(case_id),
    event_seq    INTEGER NOT NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN
                     ('case_opened','reviewer_assigned','reviewer_assignment_blocked','evidence_added',
                      'decision_recorded','appeal_submitted','appeal_reviewer_assigned','appeal_decided','case_closed')),
    event_at     TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    entity_type  TEXT,
    entity_id    TEXT,
    details      TEXT,
    UNIQUE (case_id, event_seq)
);

CREATE INDEX idx_submission_learner ON submission(learner_id);
CREATE INDEX idx_case_reviewer_case ON case_reviewer(case_id);
CREATE INDEX idx_evidence_case ON evidence_item(case_id);
CREATE INDEX idx_decision_case ON decision(case_id);
CREATE INDEX idx_event_case ON case_event(case_id);
CREATE INDEX idx_event_entity ON case_event(entity_id);
