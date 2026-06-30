# Service Level Objectives

ReleasePulse watches other apps, so it has two kinds of SLO:

- **Monitored-service SLOs** - is the thing we check healthy and fast? These are
  about the *targets*, derived from the checks the worker runs.
- **Platform SLOs** - is the watchdog itself doing its job? These are about
  *ReleasePulse* (worker up, webhooks ingested).

Every SLO is an SLI (a `good events / valid events` ratio) measured against a
target over a window. The leftover, `1 - target`, is the **error budget**: the
amount of failure allowed before the objective is missed.

The SLIs are Prometheus recording rules in
`deploy/helm/releasepulse/templates/prometheus-rules-configmap.yaml`; the
`ReleasePulse - SLOs` Grafana dashboard graphs them against the targets below.

## Objectives

| SLO | SLI (recording rule) | Target | Window |
|-----|----------------------|--------|--------|
| Monitored: check success | `sli:check_success:ratio_rate1h` | **99%** of checks succeed | 30 days |
| Monitored: check latency | `sli:check_latency_under_slo:ratio_rate1h` | **95%** of checks complete under 1s | 30 days |
| Platform: webhook ingestion | `sli:webhook_success:ratio_rate1h` | **99.9%** of webhooks accepted (received or duplicate) | 30 days |
| Platform: worker availability | `sli:worker_up:ratio_rate1h` | **99%** of scrapes the worker is up | 30 days |

Notes on the SLI definitions:

- **check success** counts `result="success"` over all checks. A genuinely-down
  target legitimately burns this budget - that is the signal, not noise.
- **check latency** uses the `le="1.0"` histogram bucket (a default
  `prometheus_client` bucket; there is no 2.0 bucket). "Fast" = under one second.
- **webhook ingestion** treats `received` and `duplicate` as good - a duplicate
  is a successful idempotent re-delivery. Only `rejected` (bad signature, replay,
  unknown service) spends budget.
- **worker availability** is `avg_over_time(up{component="worker"})` - the
  fraction of scrape intervals Prometheus saw the worker target up.

## Error budget

For a 99% SLO the budget is 1% of valid events. The dashboard shows a **burn
rate**: `(1 - SLI) / (1 - target)`.

- Burn **< 1**: spending budget slower than allowed - on track.
- Burn **= 1**: spending exactly at the rate that exhausts the budget over the
  window.
- Burn **> 1**: over budget - at this rate the objective will be missed.

Burn is **observed, not auto-actioned**. There is deliberately no Alertmanager
and no automated error-budget enforcement (that is on the spec's OUT-of-MVP
list). Reading the burn is a human signal to slow down risky changes or
investigate, using the runbooks in `docs/runbooks/`.

## A note on the measurement window

The objectives are stated over **30 days**, the standard SLO window. The
platform's own Prometheus is intentionally **ephemeral** (emptyDir, `retention:
6h` in values) because it only holds coarse self-metrics. So the dashboard shows
a rolling **1h** SLI, not a true 30-day compliance figure - the 30-day number is
the *objective* the 1h SLI is judged against, not something this Prometheus can
retain. A production deployment would point Prometheus at durable storage (or
remote-write to a long-term store) to compute real 30-day compliance.
