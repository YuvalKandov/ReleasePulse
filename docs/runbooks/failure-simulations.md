# Failure simulations

A runbook you have never executed is a guess. These are safe, reversible ways to
inject each failure on the kind cluster so you can walk the matching runbook and
confirm its Diagnose/Fix steps actually match reality.

Run them one at a time, and always run the **Revert** step after.

> ArgoCD self-heal is on. Scaling a Deployment with `kubectl scale` is reverted
> by the next reconcile, which is fine for a short drill - but if a simulation
> seems to "fix itself", that is ArgoCD, not magic. For longer drills, pause
> auto-sync first (`kubectl -n argocd patch application releasepulse --type merge
> -p '{"spec":{"syncPolicy":{"automated":null}}}'`) and restore it after.

---

## 1. Worker stops producing checks

Validates [worker-not-producing-checks.md](worker-not-producing-checks.md).

**Inject** - scale the worker to zero so the check loop stops:
```bash
kubectl -n releasepulse scale deploy/releasepulse-worker --replicas=0
```
**Observe:** the worker pod disappears, `max(checked_at)` in `checks` stops
advancing, and `sentinel_checks_total` goes flat. (To see the `/readyz`
`heartbeat stale` path instead, leave the pod up but exec in and pause the
scheduler - simpler is to kill Postgres, simulation 4, which drives
`database unreachable`.)

**Revert:**
```bash
kubectl -n releasepulse scale deploy/releasepulse-worker --replicas=1
```
Confirm `max(checked_at)` advances again within a check interval.

---

## 2. A real regression -> incident -> alert

Validates [alerts-not-firing.md](alerts-not-firing.md) (and exercises the whole
loop). The demo target's latency and error rate are tunable at runtime via
`POST /admin/mode`.

**Inject** - port-forward the demo and degrade it:
```bash
kubectl -n releasepulse port-forward deploy/releasepulse-demo 8080:8080 >/dev/null 2>&1 &
curl -s -X POST localhost:8080/admin/mode -H 'content-type: application/json' \
  -d '{"latency_ms": 800, "error_rate": 0.3}'
kill %1
```
Then fire a deployment webhook for the demo service (so the detector has a
release to evaluate). After `warmup + observation_window`, `evaluate_due` should
produce `evaluated_regression`, an incident, and - if Telegram is configured - an
alert.

**Observe:** walk the diagnose queries in the alerts runbook (`incidents`, then
the `alerts` outbox). With no Telegram creds set, confirm the *expected* "no
dispatch" path; with creds set, confirm `status='sent'`.

**Revert** - return the demo to healthy:
```bash
kubectl -n releasepulse port-forward deploy/releasepulse-demo 8080:8080 >/dev/null 2>&1 &
curl -s -X POST localhost:8080/admin/mode -H 'content-type: application/json' \
  -d '{"latency_ms": 0, "error_rate": 0.0}'
kill %1
```

---

## 3. Alert delivery failure

Validates the `failed`/`last_error` path in
[alerts-not-firing.md](alerts-not-firing.md).

**Inject** - set a deliberately invalid Telegram bot token so delivery fails
(set `secrets.telegramBotToken` to a bogus value and let it roll), then trigger a
regression as in simulation 2.

**Observe:** the `alerts` row goes `status='failed'` with `last_error` populated,
`attempts` increments each `dispatch_alerts` tick, and
`sentinel_alerts_total{result="failed"}` rises.

**Revert:** restore the real token; the outbox retries and flips to `sent`.

---

## 4. Database unreachable

Validates the `database unreachable` path in
[worker-not-producing-checks.md](worker-not-producing-checks.md) and is the
precondition drill for [db-restore.md](db-restore.md).

**Inject** - delete the Postgres pod (the StatefulSet recreates it, so this is a
brief outage, not data loss - the PVC persists):
```bash
kubectl -n releasepulse delete pod releasepulse-postgres-0
```
**Observe:** during the gap the worker `/readyz` reports `database unreachable`
and its pod goes `0/1`; the api `/readyz` fails too.

**Revert:** nothing to do - the StatefulSet brings `releasepulse-postgres-0` back
on its PVC and both probes clear. Confirm with
`kubectl -n releasepulse get pods`.

---

## 5. Backup / restore drill

Validates [db-restore.md](db-restore.md) directly - this is the periodic "are
backups actually restorable" exercise. Run the backup CronJob manually, then the
verify-restore procedure (runbook section 1) into the scratch database. Record
the date on the runbook's **Last tested** line each time.

---

## What "passing" looks like

For each simulation: the symptom appeared as the runbook describes, the diagnose
commands surfaced the right cause, and the revert restored normal operation
(worker `1/1`, `checks` advancing, dashboards live). If a runbook step did not
match what you saw, fix the runbook - that is the whole point of the drill.
