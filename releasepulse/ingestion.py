"""Deployment ingestion helpers.

The deploy boundary drives the whole before/after comparison, so we don't blindly
trust the caller's clock. `effective_deployed_at` is the caller's reported time
only when it falls within a sane window of when we received the webhook;
otherwise we fall back to the trustworthy received time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# How far reported_deployed_at may sit from received_at and still be trusted.
# Past: a deploy can finish a while before its webhook lands (retries, queueing).
# Future: only a small allowance for clock skew between sender and us.
DEFAULT_MAX_PAST = timedelta(hours=24)
DEFAULT_MAX_FUTURE = timedelta(minutes=5)


def compute_effective_deployed_at(
    reported: datetime | None,
    received: datetime,
    *,
    max_past: timedelta = DEFAULT_MAX_PAST,
    max_future: timedelta = DEFAULT_MAX_FUTURE,
) -> datetime:
    """Pick the timestamp the detector should treat as the deploy boundary.

    Returns `reported` when it lies within [received - max_past, received + max_future];
    otherwise returns `received`. A naive `reported` is assumed to be UTC.
    """
    if reported is None:
        return received
    if reported.tzinfo is None:
        reported = reported.replace(tzinfo=timezone.utc)
    if reported > received + max_future:
        return received
    if reported < received - max_past:
        return received
    return reported
