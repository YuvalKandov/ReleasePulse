"""Unit tests for effective_deployed_at selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from releasepulse.ingestion import compute_effective_deployed_at

UTC = timezone.utc
RECEIVED = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)


def test_missing_reported_falls_back_to_received() -> None:
    assert compute_effective_deployed_at(None, RECEIVED) == RECEIVED


def test_recent_reported_is_trusted() -> None:
    reported = RECEIVED - timedelta(minutes=1)
    assert compute_effective_deployed_at(reported, RECEIVED) == reported


def test_small_future_skew_is_trusted() -> None:
    reported = RECEIVED + timedelta(minutes=2)
    assert compute_effective_deployed_at(reported, RECEIVED) == reported


def test_far_future_reported_falls_back_to_received() -> None:
    reported = RECEIVED + timedelta(minutes=10)
    assert compute_effective_deployed_at(reported, RECEIVED) == RECEIVED


def test_far_past_reported_falls_back_to_received() -> None:
    reported = RECEIVED - timedelta(days=2)
    assert compute_effective_deployed_at(reported, RECEIVED) == RECEIVED


def test_past_boundary_is_inclusive() -> None:
    reported = RECEIVED - timedelta(hours=24)
    assert compute_effective_deployed_at(reported, RECEIVED) == reported


def test_future_boundary_is_inclusive() -> None:
    reported = RECEIVED + timedelta(minutes=5)
    assert compute_effective_deployed_at(reported, RECEIVED) == reported


def test_naive_reported_is_treated_as_utc() -> None:
    naive = datetime(2026, 6, 26, 11, 59, 0)  # no tzinfo
    expected = naive.replace(tzinfo=UTC)
    assert compute_effective_deployed_at(naive, RECEIVED) == expected
