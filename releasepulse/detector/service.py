"""Database-facing detector orchestration.

evaluate_deployment() is the entrypoint: given a pending deployment, it evaluates
every matching endpoint, persists one verdict row each, sets the deployment status,
and opens an incident when any endpoint regressed. Idempotent: a deployment that is
already evaluated is returned untouched.

Windows are half-open: baseline = [T - baseline_window, T), observation =
[T + warmup, T + warmup + observation_window). The exact deploy instant T is thus
excluded from the baseline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from releasepulse.detector.core import (
    DEFAULT_THRESHOLDS,
    REGRESSION_OUTCOMES,
    CheckSample,
    EndpointEvaluation,
    Outcome,
    Thresholds,
    evaluate_endpoint,
    merge_thresholds,
)
from releasepulse.models import (
    Check,
    Deployment,
    DeploymentEndpointEvaluation,
    Endpoint,
    Incident,
)


def evaluate_deployment(
    db: Session,
    deployment_id: UUID,
    *,
    defaults: Thresholds = DEFAULT_THRESHOLDS,
) -> Deployment:
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise ValueError(f"deployment {deployment_id} not found")
    # Idempotency: only a pending deployment is evaluated.
    if deployment.evaluation_status != "pending":
        return deployment

    t0 = deployment.effective_deployed_at
    baseline_start = t0 - defaults.baseline_window
    baseline_end = t0
    obs_start = t0 + defaults.warmup
    obs_end = obs_start + defaults.observation_window

    endpoints = db.scalars(
        select(Endpoint).where(
            Endpoint.service_id == deployment.service_id,
            Endpoint.environment == deployment.environment,
            Endpoint.enabled.is_(True),
            Endpoint.deleted_at.is_(None),
        )
    ).all()

    results: list[EndpointEvaluation] = []
    findings: list[tuple[Endpoint, EndpointEvaluation]] = []
    for endpoint in endpoints:
        thresholds = merge_thresholds(
            defaults,
            latency_pct=endpoint.latency_pct,
            latency_floor_ms=endpoint.latency_floor_ms,
            error_delta=endpoint.error_delta,
        )
        baseline = _window_samples(db, endpoint.id, baseline_start, baseline_end)
        observation = _window_samples(db, endpoint.id, obs_start, obs_end)
        result = evaluate_endpoint(baseline, observation, thresholds)
        results.append(result)
        db.add(_evaluation_row(deployment.id, endpoint.id, result))
        if result.outcome in REGRESSION_OUTCOMES:
            findings.append((endpoint, result))

    now = datetime.now(timezone.utc)
    deployment.evaluated_at = now

    if not endpoints:
        deployment.evaluation_status = "insufficient_baseline"
        deployment.evaluation_reason = "no_enabled_endpoints"
    elif findings:
        deployment.evaluation_status = "evaluated_regression"
        deployment.evaluation_reason = f"{len(findings)} endpoint(s) regressed"
        db.add(
            Incident(
                service_id=deployment.service_id,
                deployment_id=deployment.id,
                environment=deployment.environment,
                detected_at=now,
                status="open",
                summary=_incident_summary(deployment, findings),
            )
        )
    elif any(r.outcome == Outcome.NO_REGRESSION for r in results):
        deployment.evaluation_status = "evaluated_no_regression"
        deployment.evaluation_reason = None
    else:
        deployment.evaluation_status = "insufficient_baseline"
        deployment.evaluation_reason = _all_blocked_reason(results)

    db.commit()
    db.refresh(deployment)
    return deployment


def _window_samples(
    db: Session, endpoint_id: UUID, start: datetime, end: datetime
) -> list[CheckSample]:
    rows = db.execute(
        select(Check.success, Check.latency_ms).where(
            Check.endpoint_id == endpoint_id,
            Check.checked_at >= start,
            Check.checked_at < end,
        )
    ).all()
    return [CheckSample(success=r.success, latency_ms=r.latency_ms) for r in rows]


def _evaluation_row(
    deployment_id: UUID, endpoint_id: UUID, result: EndpointEvaluation
) -> DeploymentEndpointEvaluation:
    return DeploymentEndpointEvaluation(
        deployment_id=deployment_id,
        endpoint_id=endpoint_id,
        outcome=result.outcome,
        baseline_median_latency_ms=result.baseline_median_latency_ms,
        observed_median_latency_ms=result.observed_median_latency_ms,
        baseline_error_rate=result.baseline_error_rate,
        observed_error_rate=result.observed_error_rate,
        baseline_samples=result.baseline_samples,
        observed_samples=result.observed_samples,
        observed_failed_checks=result.observed_failed_checks,
    )


def _incident_summary(
    deployment: Deployment, findings: list[tuple[Endpoint, EndpointEvaluation]]
) -> str:
    version = deployment.version or deployment.commit_sha or "unknown"
    parts = []
    for endpoint, result in findings:
        bits = []
        if result.outcome in (Outcome.REGRESSED_LATENCY, Outcome.REGRESSED_BOTH):
            bits.append(
                f"latency {result.baseline_median_latency_ms}ms"
                f"->{result.observed_median_latency_ms}ms"
            )
        if result.outcome in (Outcome.REGRESSED_ERROR, Outcome.REGRESSED_BOTH):
            bits.append(
                f"errors {result.observed_failed_checks}/{result.observed_samples}"
            )
        parts.append(f"{endpoint.url} ({', '.join(bits)})")
    return f"Possible regression after deploy {version}: " + "; ".join(parts)


def _all_blocked_reason(results: list[EndpointEvaluation]) -> str:
    outcomes = {r.outcome for r in results}
    if outcomes == {Outcome.BASELINE_DEGRADED}:
        return "baseline_degraded_all_endpoints"
    sample_guards = {
        Outcome.INSUFFICIENT_BASELINE,
        Outcome.INSUFFICIENT_OBSERVATION,
        Outcome.INSUFFICIENT_SUCCESSFUL_BASELINE,
    }
    if outcomes <= sample_guards:
        return "insufficient_samples_all_endpoints"
    return "all_endpoints_blocked"
