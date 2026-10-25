from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "proof-artifacts" / "v2-7d1f3cc-production-cohort" / "run_production_cohort.py"
spec = importlib.util.spec_from_file_location("cohort_7d1f3cc_quality", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_labeled_s_tokens_count_as_the_same_source_ids() -> None:
    result = {
        "status": "ANALYSIS_COMPLETE",
        "clarification_needed": False,
        "summary": "x" * 10,
        "evidence_refs": ["S1", "S2", "S3"],
    }
    assert runner.quality_checks("genesis-commercial-growth", result)["evidence"] is True
    labeled = dict(result)
    labeled["evidence_refs"] = ["S1: 100 visits", "S2: 20 trials", "S3: 5 conversions"]
    assert runner.quality_checks("genesis-commercial-growth", labeled)["evidence"] is True
    extra = dict(result)
    extra["evidence_refs"] = ["S1", "S2", "S4"]
    assert runner.quality_checks("genesis-commercial-growth", extra)["evidence"] is False
    empty = dict(result)
    empty["evidence_refs"] = []
    assert runner.quality_checks("genesis-commercial-growth", empty)["evidence"] is False
    unlabeled_junk = dict(result)
    unlabeled_junk["evidence_refs"] = ["visits", "S2"]
    assert runner.quality_checks("genesis-commercial-growth", unlabeled_junk)["evidence"] is False
