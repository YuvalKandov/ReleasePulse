"""Prometheus self-metrics: the platform's own observability plane.

This is the watchdog watching itself (spec ~4). It is deliberately separate from
the product data in Postgres: Postgres answers "which endpoint regressed, when,
why"; these metrics answer coarse fleet-wide questions ("how many checks failed",
"how many alerts went out").

Cardinality discipline (spec ~10): low-cardinality labels ONLY. Never a URL,
commit SHA, incident id, environment, or error message as a label - each distinct
value is a new time series, and unbounded labels eventually sink Prometheus. The
only label here is `result`, and every metric's result set is small and bounded.

These objects register on prometheus_client's default registry at import. api and
worker are separate processes with separate registries, so each metric only
carries values in the process that increments it (checks/detector/alerts in the
worker; webhooks in the api). A counter with labels emits no series until a label
combination is first used, which is why unused metrics simply don't appear in a
given process's /metrics output.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

__all__ = [
    "CONTENT_TYPE_LATEST",
    "generate_latest",
    "checks_total",
    "check_duration_seconds",
    "detector_evaluations_total",
    "alerts_total",
    "webhooks_total",
]

# Worker: one increment per HTTP check, labelled success|failure.
checks_total = Counter(
    "sentinel_checks_total",
    "HTTP checks performed by the worker, by outcome.",
    ["result"],
)

# Worker: latency of an individual check. Unlabelled - a histogram already fans
# out into per-bucket series, so adding a label would multiply that.
check_duration_seconds = Histogram(
    "sentinel_check_duration_seconds",
    "Latency of an individual HTTP check, in seconds.",
)

# Worker: one increment per deployment the detector finishes, labelled by the
# resulting evaluation_status (a small controlled set).
detector_evaluations_total = Counter(
    "sentinel_detector_evaluations_total",
    "Deployments evaluated by the detector, by resulting status.",
    ["result"],
)

# Worker: one increment per alert delivery attempt, labelled sent|failed.
alerts_total = Counter(
    "sentinel_alerts_total",
    "Alert delivery attempts, by outcome.",
    ["result"],
)

# API: one increment per deployment webhook, labelled received|duplicate|rejected.
webhooks_total = Counter(
    "sentinel_webhooks_total",
    "Deployment webhooks handled, by outcome.",
    ["result"],
)
