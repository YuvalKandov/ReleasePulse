# Runbook: alerts not firing

**Symptom:** the detector flagged a deployment `evaluated_regression` and an
incident exists, but no Telegram alert arrived.

Background: alerting is an **outbox**. The detector writes an `incidents` row; the
worker's `dispatch_alerts` job (every 30s) finds incidents lacking a sent alert,
calls the `AlertSender`, and records the attempt in `alerts`
(`status` pending|sent|failed, `attempts`, `last_error`, `UNIQUE(incident_id, channel)`).
So "no alert" is one of: no incident, dispatch not running, or delivery failing.

## Diagnose

**1. Is there actually an incident?** No incident -> nothing to alert on; this is
really a detector question (see
[false-positive-regression.md](false-positive-regression.md) for the inverse).
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT id, deployment_id, created_at FROM incidents ORDER BY created_at DESC LIMIT 5;"
```

**2. What does the outbox say?** This is the key query - it tells you *which* of
the three failure modes you are in:
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT incident_id, channel, status, attempts, last_error
   FROM alerts ORDER BY id DESC LIMIT 5;"
```
- **No `alerts` row at all** -> the `dispatch_alerts` job is not running or not
  finding the incident.
- **status `failed`, `last_error` populated** -> delivery is failing; the error
  text is the cause (bad token, wrong chat id, network).
- **status `sent`** -> we delivered it; the problem is on the Telegram side
  (wrong chat, muted), not here.

**3. Is dispatch even enabled?** The `dispatch_alerts` job is only registered when
Telegram credentials are set. If `telegramBotToken`/`telegramChatId` are empty,
the worker never dispatches (by design - no channel configured).
```bash
kubectl -n releasepulse logs deploy/releasepulse-worker | grep -i "dispatch\|alert"
```

**4. Cross-check the metric:** `sentinel_alerts_total{result="failed"}` rising
confirms repeated delivery failures; `result="sent"` confirms success.

## Fix

- **Credentials missing/wrong:** set `secrets.telegramBotToken` and
  `secrets.telegramChatId` (via `--set`, a sealed secret, or the secret the chart
  references) and let ArgoCD sync. Because secret changes only roll pods via the
  `checksum/*` annotation, confirm the worker pod actually restarted, or
  `kubectl -n releasepulse rollout restart deploy/releasepulse-worker`.
- **Transient delivery failure:** the outbox retries automatically while
  `attempts < max_attempts` and `status` is pending|failed - no action needed
  once connectivity returns. A row stuck at max attempts needs the root cause
  fixed; you can reset it to retry:
  ```bash
  kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
    psql -U postgres -d releasepulse -c \
    "UPDATE alerts SET status='pending', attempts=0 WHERE id=<id>;"
  ```
- **Delivered but not seen:** verify the bot is in the target chat and the
  `chat_id` is correct (group ids are negative).

## Verify

After the next `dispatch_alerts` tick (<=30s):
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT status, attempts, last_error FROM alerts ORDER BY id DESC LIMIT 1;"
```
`status='sent'`, `last_error` NULL, and the message visible in Telegram.
