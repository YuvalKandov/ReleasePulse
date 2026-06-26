"""Pure per-endpoint regression logic (no database, no clock, no I/O).

Given the baseline and observation check samples for one endpoint plus the
thresholds, decide the outcome. Kept pure so every guard and regression path is
trivially unit-testable. The database-facing orchestration lives in service.py.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import NamedTuple


class Outcome:
    """The controlled outcome vocabulary (matches the DB CHECK constraint)."""

    REGRESSED_LATENCY = "regressed_latency"
    REGRESSED_ERROR = "regressed_error"
    REGRESSED_BOTH = "regressed_both"
    NO_REGRESSION = "no_regression"
    INSUFFICIENT_BASELINE = "insufficient_baseline"
    INSUFFICIENT_OBSERVATION = "insufficient_observation"
    INSUFFICIENT_SUCCESSFUL_BASELINE = "insufficient_successful_baseline"
    BASELINE_DEGRADED = "baseline_degraded"
    SUPERSEDED = "superseded"


REGRESSION_OUTCOMES = frozenset(
    {Outcome.REGRESSED_LATENCY, Outcome.REGRESSED_ERROR, Outcome.REGRESSED_BOTH}
)


@dataclass(frozen=True)
class Thresholds:
    """Detector configuration. Time fields drive window gathering (orchestration);
    the rest drive the comparison here. Defaults are the spec defaults."""

    baseline_window: timedelta = timedelta(minutes=10)
    warmup: timedelta = timedelta(minutes=3)
    observation_window: timedelta = timedelta(minutes=10)
    min_samples: int = 15
    min_successful_baseline: int = 10
    baseline_degraded_rate: float = 0.20
    latency_pct: float = 0.50
    latency_floor_ms: int = 50
    error_delta: float = 0.05
    min_failures: int = 2


DEFAULT_THRESHOLDS = Thresholds()


def merge_thresholds(
    base: Thresholds,
    *,
    latency_pct: float | None = None,
    latency_floor_ms: int | None = None,
    error_delta: float | None = None,
) -> Thresholds:
    """Apply per-endpoint overrides (None = keep the base value)."""
    return replace(
        base,
        latency_pct=base.latency_pct if latency_pct is None else float(latency_pct),
        latency_floor_ms=base.latency_floor_ms if latency_floor_ms is None else int(latency_floor_ms),
        error_delta=base.error_delta if error_delta is None else float(error_delta),
    )


class CheckSample(NamedTuple):
    """The only two check fields the comparison needs."""

    success: bool
    latency_ms: int | None


@dataclass(frozen=True)
class EndpointEvaluation:
    """Everything needed to persist one deployment_endpoint_evaluation row."""

    outcome: str
    baseline_samples: int
    observed_samples: int
    observed_failed_checks: int
    baseline_median_latency_ms: int | None
    observed_median_latency_ms: int | None
    baseline_error_rate: float | None
    observed_error_rate: float | None


def _median(values: Sequence[int]) -> float | None:
    return statistics.median(values) if values else None


def _to_int(value: float | None) -> int | None:
    return None if value is None else round(value)


def evaluate_endpoint(
    baseline: Sequence[CheckSample],
    observation: Sequence[CheckSample],
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> EndpointEvaluation:
    """Return the verdict for one endpoint around one deployment."""
    t = thresholds

    b_total = len(baseline)
    o_total = len(observation)
    b_failed = sum(1 for c in baseline if not c.success)
    o_failed = sum(1 for c in observation if not c.success)
    b_success = b_total - b_failed
    b_error_rate = b_failed / b_total if b_total else None
    o_error_rate = o_failed / o_total if o_total else None
    b_median = _median([c.latency_ms for c in baseline if c.success and c.latency_ms is not None])
    o_median = _median([c.latency_ms for c in observation if c.success and c.latency_ms is not None])

    def verdict(outcome: str) -> EndpointEvaluation:
        return EndpointEvaluation(
            outcome=outcome,
            baseline_samples=b_total,
            observed_samples=o_total,
            observed_failed_checks=o_failed,
            baseline_median_latency_ms=_to_int(b_median),
            observed_median_latency_ms=_to_int(o_median),
            baseline_error_rate=b_error_rate,
            observed_error_rate=o_error_rate,
        )

    # Terminal guards, in order. Any match ends the endpoint with no regression.
    if b_total < t.min_samples:
        return verdict(Outcome.INSUFFICIENT_BASELINE)
    if o_total < t.min_samples:
        return verdict(Outcome.INSUFFICIENT_OBSERVATION)
    if b_success < t.min_successful_baseline:
        return verdict(Outcome.INSUFFICIENT_SUCCESSFUL_BASELINE)

    # Two independent dimensions.
    error_eligible = b_error_rate < t.baseline_degraded_rate  # else baseline_degraded
    latency_eligible = b_median is not None and o_median is not None

    latency_regressed = (
        latency_eligible
        and o_median >= b_median * (1 + t.latency_pct)
        and (o_median - b_median) >= t.latency_floor_ms
    )
    error_regressed = (
        error_eligible
        and (o_error_rate - b_error_rate) >= t.error_delta
        and o_failed >= t.min_failures
    )

    if latency_regressed and error_regressed:
        return verdict(Outcome.REGRESSED_BOTH)
    if latency_regressed:
        return verdict(Outcome.REGRESSED_LATENCY)
    if error_regressed:
        return verdict(Outcome.REGRESSED_ERROR)
    # No regression. If at least one dimension was assessable, the endpoint is fine;
    # otherwise the baseline was degraded and nothing could be judged.
    if latency_eligible or error_eligible:
        return verdict(Outcome.NO_REGRESSION)
    return verdict(Outcome.BASELINE_DEGRADED)
