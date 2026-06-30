# Runbooks

Operational procedures for ReleasePulse on Kubernetes. Each runbook follows the
same shape: **Symptom -> Diagnose -> Fix -> Verify**.

| Runbook | Use when |
|---------|----------|
| [worker-not-producing-checks.md](worker-not-producing-checks.md) | No new `checks` rows; dashboards flat; worker pod not ready. |
| [alerts-not-firing.md](alerts-not-firing.md) | A regression was detected but no Telegram alert arrived. |
| [false-positive-regression.md](false-positive-regression.md) | A deployment was flagged `evaluated_regression` but the app was fine. |
| [db-restore.md](db-restore.md) | Database lost or corrupted, or verifying that backups restore. |

To exercise these against real failures, see
[failure-simulations.md](failure-simulations.md).

## Orientation

The platform runs in the `releasepulse` namespace, delivered by ArgoCD from git
(changes ship by `git push`, not `helm upgrade` or `kubectl edit` - ArgoCD
self-heal reverts manual drift). Key objects:

- `deploy/releasepulse-api` - FastAPI: registration, webhook ingestion (:8000).
- `deploy/releasepulse-worker` - single-replica APScheduler loop: checks,
  auto-evaluation, alert dispatch. Internal HTTP on :8001 (`/healthz` `/readyz`
  `/metrics`).
- `sts/releasepulse-postgres` - the database (:5432).
- `deploy/releasepulse-demo` - the configurable check target (:8080).
- `deploy/releasepulse-prometheus`, `deploy/releasepulse-grafana` - the
  observability plane. Grafana at `http://grafana.localhost`.

The worker's three periodic jobs and their intervals: `reconcile` (60s, also
emits the heartbeat), `evaluate_due` (30s), `dispatch_alerts` (30s).
