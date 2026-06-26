"""Integration tests for evaluate_deployment against seeded Postgres.

This is the spec's first concrete target: seeded checks + a deployment row ->
correct deployment status, per-endpoint evaluation rows, and an incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from releasepulse.detector.service import evaluate_deployment
from releasepulse.models import (
    Check,
    Deployment,
    DeploymentEndpointEvaluation,
    Endpoint,
    Incident,
    Service,
)

UTC = timezone.utc
T = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the deploy boundary

BASELINE_START = T - timedelta(minutes=10)
OBS_START = T + timedelta(minutes=3)


# --- seed helpers ---------------------------------------------------------

def _service(db, name="svc") -> Service:
    s = Service(name=name)
    db.add(s)
    db.flush()
    return s


def _endpoint(db, service_id, url="https://svc/api", environment="production",
              enabled=True, deleted=False, **overrides) -> Endpoint:
    e = Endpoint(
        service_id=service_id,
        url=url,
        environment=environment,
        enabled=enabled,
        deleted_at=datetime.now(UTC) if deleted else None,
        **overrides,
    )
    db.add(e)
    db.flush()
    return e


def _deployment(db, service_id, environment="production", external_id="e:1") -> Deployment:
    d = Deployment(
        service_id=service_id,
        environment=environment,
        source="manual",
        external_id=external_id,
        received_at=T,
        effective_deployed_at=T,
        evaluation_status="pending",
    )
    db.add(d)
    db.flush()
    return d


def _seed_window(db, endpoint_id, start, *, n_success, latency, n_fail,
                 spacing=timedelta(seconds=10)) -> None:
    t = start
    for _ in range(n_success):
        db.add(Check(endpoint_id=endpoint_id, checked_at=t, success=True, latency_ms=latency))
        t += spacing
    for _ in range(n_fail):
        db.add(Check(endpoint_id=endpoint_id, checked_at=t, success=False, error_type="internal_error"))
        t += spacing


def _evals(db, deployment_id):
    return db.scalars(
        select(DeploymentEndpointEvaluation).where(
            DeploymentEndpointEvaluation.deployment_id == deployment_id
        )
    ).all()


def _incident_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Incident))


# --- tests ----------------------------------------------------------------

def test_clean_deploy_is_no_regression(db) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    dep = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "evaluated_no_regression"
    evals = _evals(db, dep.id)
    assert len(evals) == 1 and evals[0].outcome == "no_regression"
    assert _incident_count(db) == 0


def test_latency_regression_opens_incident(db) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    dep = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=200, n_fail=0)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "evaluated_regression"
    evals = _evals(db, dep.id)
    assert len(evals) == 1 and evals[0].outcome == "regressed_latency"
    incident = db.scalar(select(Incident))
    assert incident is not None and incident.deployment_id == dep.id
    assert "latency 100ms->200ms" in incident.summary


def test_error_regression_opens_incident(db) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    dep = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=17, latency=100, n_fail=3)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "evaluated_regression"
    assert _evals(db, dep.id)[0].outcome == "regressed_error"
    assert _incident_count(db) == 1


def test_insufficient_baseline_blocks_without_incident(db) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    dep = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=10, latency=100, n_fail=0)  # < 15
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "insufficient_baseline"
    assert result.evaluation_reason == "insufficient_samples_all_endpoints"
    assert _evals(db, dep.id)[0].outcome == "insufficient_baseline"
    assert _incident_count(db) == 0


def test_multiple_endpoints_only_regressed_one_is_a_finding(db) -> None:
    svc = _service(db)
    ep_bad = _endpoint(db, svc.id, url="https://svc/slow")
    ep_ok = _endpoint(db, svc.id, url="https://svc/fine")
    dep = _deployment(db, svc.id)
    for ep, obs_latency in ((ep_bad, 200), (ep_ok, 100)):
        _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
        _seed_window(db, ep.id, OBS_START, n_success=20, latency=obs_latency, n_fail=0)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "evaluated_regression"
    # One row per endpoint (always), but the incident's findings = only the regressed one.
    assert len(_evals(db, dep.id)) == 2
    regressed = [e for e in _evals(db, dep.id) if e.endpoint_id == ep_bad.id]
    assert regressed[0].outcome == "regressed_latency"
    fine = [e for e in _evals(db, dep.id) if e.endpoint_id == ep_ok.id]
    assert fine[0].outcome == "no_regression"
    assert _incident_count(db) == 1


def test_rerun_is_idempotent(db) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    dep = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=200, n_fail=0)
    db.commit()

    evaluate_deployment(db, dep.id)
    evaluate_deployment(db, dep.id)  # second run must not duplicate anything

    assert len(_evals(db, dep.id)) == 1
    assert _incident_count(db) == 1


def test_other_environment_endpoint_is_ignored(db) -> None:
    svc = _service(db)
    prod = _endpoint(db, svc.id, url="https://svc/api", environment="production")
    staging = _endpoint(db, svc.id, url="https://svc/api", environment="staging")
    dep = _deployment(db, svc.id, environment="production")
    # production endpoint stable; staging endpoint would regress (but must be skipped).
    _seed_window(db, prod.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, prod.id, OBS_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, staging.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, staging.id, OBS_START, n_success=20, latency=500, n_fail=0)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    assert result.evaluation_status == "evaluated_no_regression"
    evals = _evals(db, dep.id)
    assert len(evals) == 1 and evals[0].endpoint_id == prod.id


def test_disabled_endpoint_is_ignored(db) -> None:
    svc = _service(db)
    _endpoint(db, svc.id, url="https://svc/off", enabled=False)
    dep = _deployment(db, svc.id)
    db.commit()

    result = evaluate_deployment(db, dep.id)

    # No evaluable endpoints at all.
    assert result.evaluation_status == "insufficient_baseline"
    assert result.evaluation_reason == "no_enabled_endpoints"
    assert _evals(db, dep.id) == []
