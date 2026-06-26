"""Exhaustive unit tests for the pure per-endpoint detector logic."""

from __future__ import annotations

from releasepulse.detector.core import (
    DEFAULT_THRESHOLDS,
    CheckSample,
    Outcome,
    evaluate_endpoint,
    merge_thresholds,
)


def samples(n_success: int, latency: int | None, n_fail: int) -> list[CheckSample]:
    """Build a window: n_success successful checks at a fixed latency + n_fail failures."""
    return (
        [CheckSample(True, latency) for _ in range(n_success)]
        + [CheckSample(False, None) for _ in range(n_fail)]
    )


def outcome(baseline, observation, thresholds=DEFAULT_THRESHOLDS) -> str:
    return evaluate_endpoint(baseline, observation, thresholds).outcome


# --- terminal sample guards (checked in order) ----------------------------

def test_insufficient_baseline() -> None:
    assert outcome(samples(14, 100, 0), samples(20, 100, 0)) == Outcome.INSUFFICIENT_BASELINE


def test_insufficient_observation() -> None:
    assert outcome(samples(20, 100, 0), samples(14, 100, 0)) == Outcome.INSUFFICIENT_OBSERVATION


def test_insufficient_successful_baseline() -> None:
    # 15 total but only 9 successful (< 10) -> can't trust a latency median.
    assert outcome(samples(9, 100, 6), samples(20, 100, 0)) == Outcome.INSUFFICIENT_SUCCESSFUL_BASELINE


def test_baseline_guard_precedes_successful_guard() -> None:
    # Too few total AND too few successful: the total guard wins (checked first).
    assert outcome(samples(5, 100, 5), samples(20, 100, 0)) == Outcome.INSUFFICIENT_BASELINE


# --- no regression --------------------------------------------------------

def test_stable_is_no_regression() -> None:
    assert outcome(samples(20, 100, 0), samples(20, 100, 0)) == Outcome.NO_REGRESSION


def test_latency_rise_below_pct_is_no_regression() -> None:
    # 100 -> 140 is +40%, below the 50% threshold.
    assert outcome(samples(20, 100, 0), samples(20, 140, 0)) == Outcome.NO_REGRESSION


def test_latency_rise_below_floor_is_no_regression() -> None:
    # 10 -> 30 is +200% but only +20ms, below the 50ms floor.
    assert outcome(samples(20, 10, 0), samples(20, 30, 0)) == Outcome.NO_REGRESSION


def test_error_rise_below_delta_is_no_regression() -> None:
    # 2/50 = 4% error, below the 5% delta, despite >= 2 failures.
    assert outcome(samples(20, 100, 0), samples(48, 100, 2)) == Outcome.NO_REGRESSION


def test_error_rise_with_one_failure_is_no_regression() -> None:
    # 1/20 = 5% delta met, but only 1 failure (< min_failures).
    assert outcome(samples(20, 100, 0), samples(19, 100, 1)) == Outcome.NO_REGRESSION


# --- regressions ----------------------------------------------------------

def test_latency_regression() -> None:
    assert outcome(samples(20, 100, 0), samples(20, 200, 0)) == Outcome.REGRESSED_LATENCY


def test_error_regression() -> None:
    # 3/20 = 15% error, latency flat.
    assert outcome(samples(20, 100, 0), samples(17, 100, 3)) == Outcome.REGRESSED_ERROR


def test_both_regression() -> None:
    assert outcome(samples(20, 100, 0), samples(17, 200, 3)) == Outcome.REGRESSED_BOTH


def test_latency_boundary_is_inclusive() -> None:
    # Exactly 1.5x and exactly +50ms -> regression (>= comparisons).
    assert outcome(samples(20, 100, 0), samples(20, 150, 0)) == Outcome.REGRESSED_LATENCY


def test_error_boundary_is_inclusive() -> None:
    # Exactly +5% delta with exactly 2 failures -> regression.
    assert outcome(samples(20, 100, 0), samples(38, 100, 2)) == Outcome.REGRESSED_ERROR


# --- degraded baseline behaviour ------------------------------------------

def test_degraded_baseline_still_allows_latency_no_regression() -> None:
    # baseline 4/16 = 25% errors (degraded) but 12 successful; latency flat.
    # Error dimension is blocked, latency is fine -> no_regression.
    assert outcome(samples(12, 100, 4), samples(20, 100, 0)) == Outcome.NO_REGRESSION


def test_degraded_baseline_still_catches_latency_regression() -> None:
    # Degraded baseline must not hide a latency regression.
    assert outcome(samples(12, 100, 4), samples(20, 300, 0)) == Outcome.REGRESSED_LATENCY


def test_degraded_baseline_with_no_observed_median_is_baseline_degraded() -> None:
    # Degraded baseline AND observation has no successful checks: neither
    # dimension is assessable -> baseline_degraded (the terminal fallback).
    assert outcome(samples(12, 100, 4), samples(0, None, 20)) == Outcome.BASELINE_DEGRADED


def test_degraded_baseline_does_not_attribute_errors() -> None:
    # baseline already 25% errors; observation 30% errors. The rise is real but
    # not attributable to the deploy -> error blocked, latency flat -> no_regression.
    assert outcome(samples(12, 100, 4), samples(14, 100, 6)) == Outcome.NO_REGRESSION


# --- per-endpoint overrides -----------------------------------------------

def test_override_tightens_error_delta() -> None:
    # 2/50 = 4% would be no_regression at the 5% default, but a 2% override flags it.
    tight = merge_thresholds(DEFAULT_THRESHOLDS, error_delta=0.02)
    assert outcome(samples(20, 100, 0), samples(48, 100, 2), tight) == Outcome.REGRESSED_ERROR


def test_recorded_numbers_are_populated() -> None:
    result = evaluate_endpoint(samples(20, 100, 0), samples(17, 200, 3))
    assert result.baseline_samples == 20
    assert result.observed_samples == 20
    assert result.observed_failed_checks == 3
    assert result.baseline_median_latency_ms == 100
    assert result.observed_median_latency_ms == 200
    assert result.baseline_error_rate == 0.0
    assert result.observed_error_rate == 3 / 20
