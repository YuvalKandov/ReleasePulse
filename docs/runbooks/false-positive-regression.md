# Runbook: false-positive regression

**Symptom:** a deployment was flagged `evaluated_regression` and raised an
incident, but the release was actually fine. Or the inverse: a release you
expected to be judged was not (`insufficient_baseline`, `superseded`, still
`pending`).

The detector is deterministic and records *why* it decided what it did. Start
with the evidence, not assumptions.

## Diagnose

**1. Read the deployment's verdict and reason:**
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT id, evaluation_status, evaluation_reason, effective_deployed_at
   FROM deployments ORDER BY effective_deployed_at DESC LIMIT 5;"
```
`evaluation_status` is one of `pending`, `evaluated_regression`,
`evaluated_no_regression`, `insufficient_baseline`, `superseded`, `invalid`;
`evaluation_reason` carries the detail.

**2. Read the per-endpoint evaluations** - one row per endpoint per deployment,
with the actual numbers the detector compared:
```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c \
  "SELECT endpoint_id, outcome, baseline_median_latency_ms, observed_median_latency_ms,
          baseline_error_rate, observed_error_rate
   FROM deployment_endpoint_evaluations WHERE deployment_id='<id>';"
```
An incident's findings are exactly the rows whose `outcome` is a regression.

**3. Decide: real or false positive.** The detector fires latency regression when
`observed_median >= baseline_median * 1.50` AND `observed - baseline >= 50ms`;
error regression when `observed_error_rate - baseline_error_rate >= 0.05` AND
`observed_failed >= 2`. Compare the row's numbers to those rules:
- If the observed numbers genuinely crossed the thresholds, it is a **true
  positive** - the release did regress that endpoint. Believe it.
- If the numbers are close/noisy, look at *why the baseline was unrepresentative*.

**Common false-positive causes:**
- **Thin/noisy windows.** On a tiny demo cluster the windows are shortened
  (`DETECTOR_*`), so a handful of slow checks can swing the median. The guard
  reasons `insufficient_baseline_samples`, `insufficient_observation_samples`,
  `insufficient_successful_baseline` exist precisely to skip these - if you see a
  regression on very few samples, widen the windows / `minSamples`.
- **Degraded baseline.** If the pre-deploy window was already unhealthy, the
  comparison is meaningless; the `baseline_degraded` guard (baseline error rate
  >= 0.20) should have skipped it. A regression despite a bad baseline is a sign
  the deploy timestamp is wrong.
- **Wrong deploy time.** `effective_deployed_at` anchors both windows. A webhook
  with a bad `reported_deployed_at` shifts the baseline onto post-deploy traffic.
  Check the three timestamps on the deployment row.
- **Overlapping deploys.** A second deploy inside the window truncates the first
  below `min_samples` -> status `superseded`, not a regression. If you expected a
  verdict and got `superseded`, that is why.

## Fix

- **Confirmed false positive from thin windows:** raise the detector windows /
  `minSamples` via `DETECTOR_*` config (chart `config.detector.*`) and let ArgoCD
  sync. This is config, not code.
- **Bad deploy timestamp:** fix the webhook sender (CI/ArgoCD) to send an
  accurate `reported_deployed_at`; re-sent events dedup on `(source, external_id)`
  so correcting the source is safe.
- **Acknowledge a real-but-accepted incident** rather than suppressing the
  detector - the signal was correct.

Do **not** "fix" a true positive by loosening thresholds globally. Thresholds are
per-endpoint configurable; tune the one noisy endpoint, not the detector.

## Verify

Re-evaluation is idempotent (`UNIQUE(deployment_id, endpoint_id)` and
`UNIQUE(incidents.deployment_id)`), so re-running the detector on the same
deployment will not double-count. After a config change, a *new* deployment
event should land in the correct state - watch its row reach
`evaluated_no_regression` with representative sample counts, and confirm on the
service-health dashboard that the deploy annotation does not sit on a real
latency/error jump.
