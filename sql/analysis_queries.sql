-- Analysis queries. Run all of them with: python -m src.analysis --scale full
-- Each query is preceded by a name comment line.

-- name: Funnel from submissions to closed cases
SELECT 'submissions' AS stage, COUNT(*) AS n FROM submission
UNION ALL SELECT 'similarity flags', COUNT(*) FROM similarity_report WHERE is_flagged = 1
UNION ALL SELECT 'cases opened', COUNT(*) FROM integrity_case
UNION ALL SELECT 'cases with a decision', COUNT(DISTINCT case_id) FROM decision
UNION ALL SELECT 'appeals', COUNT(*) FROM appeal
UNION ALL SELECT 'cases closed', COUNT(*) FROM integrity_case WHERE status = 'closed';

-- name: Flags and cases by score band
SELECT r.score_band, COUNT(*) AS flags, COUNT(c.case_id) AS cases,
       ROUND(AVG(r.similarity_score), 1) AS avg_score
FROM similarity_report r LEFT JOIN integrity_case c USING (similarity_report_id)
GROUP BY r.score_band ORDER BY MIN(r.similarity_score);

-- name: First-decision outcome by score band (high score does not always mean breach)
SELECT c.review_priority, d.outcome, COUNT(*) AS n
FROM integrity_case c JOIN decision d ON d.case_id = c.case_id AND d.decision_round = 1
GROUP BY c.review_priority, d.outcome ORDER BY c.review_priority, n DESC;

-- name: Outcome when quoted material or a shared template is present
SELECT CASE WHEN r.common_template_indicator = 1 THEN 'shared template'
            WHEN r.quoted_material_share >= 0.3 THEN 'mostly quoted material'
            ELSE 'neither' END AS context,
       d.outcome, COUNT(*) AS n
FROM decision d JOIN integrity_case c USING (case_id) JOIN similarity_report r USING (similarity_report_id)
WHERE d.decision_round = 1 GROUP BY context, d.outcome ORDER BY context, n DESC;

-- name: Appeal outcomes
SELECT outcome, COUNT(*) AS n FROM appeal GROUP BY outcome ORDER BY n DESC;

-- name: Reviewer workload (blocked conflict attempts excluded)
SELECT rv.reviewer_id, rv.reviewer_role, rv.department,
       SUM(cr.assignment_role = 'primary') AS primary_cases,
       SUM(cr.assignment_role = 'secondary') AS secondary_cases,
       SUM(cr.assignment_role = 'appeal') AS appeal_cases
FROM reviewer rv JOIN case_reviewer cr ON cr.reviewer_id = rv.reviewer_id AND cr.assignment_status <> 'blocked_conflict'
GROUP BY rv.reviewer_id ORDER BY (primary_cases + secondary_cases + appeal_cases) DESC LIMIT 10;

-- name: Conflict-of-interest attempts blocked by the guard
SELECT case_id, reviewer_id, assignment_role, assigned_at, status_reason
FROM case_reviewer WHERE assignment_status = 'blocked_conflict' ORDER BY assigned_at LIMIT 10;

-- name: Decision turnaround by priority (days from case open to first decision)
SELECT c.review_priority, COUNT(*) AS decisions,
       ROUND(AVG(julianday(d.decided_at) - julianday(c.opened_at)), 1) AS avg_days,
       ROUND(MAX(julianday(d.decided_at) - julianday(c.opened_at)), 1) AS max_days
FROM integrity_case c JOIN decision d ON d.case_id = c.case_id AND d.decision_round = 1
WHERE d.decision_source = 'human_review' GROUP BY c.review_priority;
