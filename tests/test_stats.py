"""Paired bootstrap: deterministic, and conservative when the signal is noise."""

from __future__ import annotations

import numpy as np

from bike_agent.stats import describe, is_significant, paired_bootstrap


def test_interval_excludes_zero_for_a_consistent_difference():
    differences = np.full(200, -0.5)
    stats = paired_bootstrap(differences)
    assert stats["significant"] is True
    assert stats["ci_high"] < 0
    assert stats["n"] == 200


def test_interval_contains_zero_for_noise():
    differences = np.tile([-1.0, 1.0], 100)  # mean is exactly zero
    stats = paired_bootstrap(differences)
    assert stats["significant"] is False
    assert stats["ci_low"] < 0 < stats["ci_high"]


def test_bootstrap_is_deterministic():
    rng = np.random.default_rng(3)
    differences = rng.normal(-0.2, 1.0, size=150)
    assert paired_bootstrap(differences) == paired_bootstrap(differences)


def test_small_samples_do_not_claim_significance():
    stats = paired_bootstrap([1.0])
    assert stats["significant"] is False
    assert stats["ci_low"] is None
    assert is_significant(stats) is False


def test_describe_reports_an_interval_or_the_reason_it_is_missing():
    assert describe(None) == ""
    assert "样本量不足" in describe(paired_bootstrap([1.0]))
    assert "95% CI" in describe(paired_bootstrap(np.full(50, -0.25)))
