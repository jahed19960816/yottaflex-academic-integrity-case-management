"""End-to-end: generate -> export -> load CSVs into SQLite -> validate -> evaluate against ground truth."""
import csv

import pytest

from src.common import load_settings
from src.generate_data import generate
from src.validation import run_validation

SETTINGS = load_settings()


@pytest.fixture(scope="module")
def starter(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    manifest = generate("starter", SETTINGS["seed"], root, SETTINGS)
    summary = run_validation("starter", root, run_repro=False, settings=SETTINGS)
    return root, manifest, summary


def test_configured_volumes(starter):
    _, manifest, _ = starter
    p = SETTINGS["scales"]["starter"]
    assert manifest["row_counts"]["submission"] == p["submissions"]
    assert manifest["row_counts"]["similarity_report"] == p["similarity_reports"]
    assert manifest["row_counts"]["integrity_case"] == p["cases"]


def test_all_operational_checks_pass(starter):
    _, _, summary = starter
    failed = [c for c in summary["checks"] if not c["passed"]]
    assert not failed, failed
    assert summary["overall_passed"]


def test_each_negative_scenario_is_detected_exactly(starter):
    _, _, summary = starter
    for issue_type, result in summary["evaluation"]["anomaly_detection"].items():
        assert result["expected"] >= 1, issue_type
        assert result["missed"] == [] and result["unexpected"] == [], issue_type


def test_ground_truth_is_kept_out_of_operational_data(starter):
    root, _, _ = starter
    labels = {"true_conduct", "flag_is_false_positive", "issue_type", "anomaly_id"}
    for path in (root / "generated" / "starter" / "csv").glob("*.csv"):
        with open(path, newline="") as fh:
            header = set(next(csv.reader(fh)))
        assert not header & labels, path.name


def test_same_seed_same_bytes_different_seed_different_bytes(tmp_path):
    a = generate("starter", 7, tmp_path / "a", SETTINGS)
    b = generate("starter", 7, tmp_path / "b", SETTINGS)
    c = generate("starter", 8, tmp_path / "c", SETTINGS)
    assert a["files"] == b["files"]
    assert a["files"] != c["files"]
