import random

from src import rules
from src.common import load_settings

RULES = load_settings()["integrity_rules"]


def test_active_rule_uses_effective_dates():
    assert rules.active_rule(RULES, "2026-02-28T23:59:59Z")["version"] == 1
    assert rules.active_rule(RULES, "2026-03-01T00:00:00Z")["version"] == 2  # effective_to is exclusive


def test_score_bands_follow_thresholds():
    v2 = RULES[1]
    assert [rules.score_band(s, v2) for s in (19.99, 20, 39.99, 40, 59.99, 60, 100)] == \
        ["none", "low", "low", "standard", "standard", "priority", "priority"]


def test_quotes_and_templates_reduce_the_signal():
    report = {"similarity_score": 70, "matched_source_count": 6, "quoted_material_share": 0.0,
              "common_template_indicator": 0}
    quoted = dict(report, quoted_material_share=0.8)
    template = dict(report, common_template_indicator=1)
    assert rules.adjusted_signal(quoted) < rules.adjusted_signal(report)
    assert rules.adjusted_signal(template) < rules.adjusted_signal(report)


def test_high_score_with_legitimate_quotes_is_not_automatically_a_breach():
    report = {"similarity_score": 75, "matched_source_count": 5, "quoted_material_share": 0.85,
              "common_template_indicator": 0}
    outcomes = {rules.simulate_review_outcome(report, random.Random(i)) for i in range(200)}
    assert "confirmed_breach" not in outcomes


def test_derive_status_replays_events():
    assert rules.derive_status(["case_opened", "reviewer_assigned", "evidence_added"]) == "under_review"
    assert rules.derive_status(["case_opened", "reviewer_assigned", "decision_recorded", "appeal_submitted"]) == "appeal_open"
    assert rules.derive_status(["case_opened", "reviewer_assigned", "decision_recorded", "case_closed"]) == "closed"
