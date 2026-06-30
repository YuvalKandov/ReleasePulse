# Runbook: worker not producing checks

**Symptom:** the service-health dashboard latency/error panels go flat, no new
rows land in `checks`, or the worker pod is `0/1` ready. Nothing is being
monitored - the platform is blind.

## Diagnose

Work from the outside in.

**1. Is the worker pod up and ready?**
```bash
kubectl -n releasepulse get pods -l app.kubernetes.io/component=worker
```
The `READY` column reflects the worker's `/readyz`. `0/1` means readiness is
failing - get the reason from the probe (it is designed to tell failure modes
apart):
```bash
kubectl -n releasepulse describe pod -l app.kubernetes.io/component=worker | grep -A3 Readiness
kubectl -n releasepulse logs deploy/releasepulse-worker --tail=50
```
`/readyz` returns one of: `database unreachable`, `scheduler not started`,
`no heartbeat yet`, `heartbeat stale`, `not initialised`.

**2. Map the reason to a cause:**
- **database unreachable** - Postgres is down or the DSN/secret is wrong. Check
  `kubectl -n releasepulse get pods -l app.kubernetes.io/component=postgres` and
  `pg_isready`.
- **scheduler not started / not initialised** - the worker crashed during
  startup (e.g. it raced an empty database before migrations). Check the logs and
  that the `migrate` Job completed.
- **heartbeat stale** - the scheduler loop is wedged. The heartbeat is emitted by
  the `reconcile` job every 60s; `/readyz` fails it after 180s (3 missed beats).
  A stuck job (e.g. a hung DB query) or a swallowed exception in the loop is the
  usual cause - look for a stack trace or a job that never returns in the logs.

**3. Is the check loop even scheduled?** A subtle failure: the worker is ready
but has **no per-endpoint check jobs**, because there are no enabled endpoints to
reconcile, or the periodic jobs were wiped. Confirm endpoints exist and are
enabled:
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT count(*) FILTER (WHERE enabled) AS enabled_endpoints FROM endpoints WHERE deleted_at IS NULL;"
```
If that is 0, there is nothing to check - register a service/endpoint (not a
fault). Historic gotcha: a `reconcile` bug once deleted the periodic jobs
themselves; they are now protected by `PERIODIC_JOB_IDS` and a regression test,
but if checks AND `evaluate_due`/`dispatch_alerts` all stopped at once, suspect
the scheduler, not the data.

**4. Confirm the metric:** check `sentinel_checks_total` in Grafana Explore
(Prometheus datasource). A flat total corroborates "no checks happening".

## Fix

- **DB down:** recover Postgres first (pod scheduling, PVC bound, node pressure);
  the worker `/readyz` clears on its own once the DB answers.
- **Crash/wedged loop:** restart the worker to clear a wedged scheduler:
  ```bash
  kubectl -n releasepulse rollout restart deploy/releasepulse-worker
  ```
  Restart is safe - checks are stateless and idempotent. If it crash-loops,
  the logs name the startup failure (commonly DB/migrations).
- **Config/secret drift:** because config and secret changes only roll pods via a
  `checksum/*` annotation, a stale pod can run old config. A `rollout restart`
  (or a real `git push` that ArgoCD syncs) repins it.

## Verify

```bash
kubectl -n releasepulse get pods -l app.kubernetes.io/component=worker   # 1/1 ready
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT max(checked_at) FROM checks;"
```
`max(checked_at)` should advance within one check interval, and
`sentinel_checks_total` should climb again on the dashboard.
