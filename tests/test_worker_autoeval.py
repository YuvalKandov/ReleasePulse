"""Integration tests for evaluate_due_deployments against seeded Postgres.

The worker's periodic auto-evaluation: a pending deployment is picked up only once
its full window (warmup + observation_window) has elapsed, then handed to the same
evaluate_deployment used everywhere else. `now` is injected so the "is it due yet"
boundary is deterministic without touching the clock.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from releasepulse.detector.service import evaluate_due_deployments
from releasepulse.models import Deployment
from tests.test_detector_service import (
    BASELINE_START,
    OBS_START,
    T,
    _deployment,
    _endpoint,
    _evals,
    _incident_count,
    _seed_window,
    _service,
)

# Due at T + warmup (3m) + observation_window (10m) = T + 13m.
NOW_DUE = T + timedelta(minutes=20)
NOW_EARLY = T + timedelta(minutes=5)


def _factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_due_clean_deploy_is_evaluated(db, engine) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    d = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
    db.commit()

    evaluated = evaluate_due_deployments(_factory(engine), now=NOW_DUE)

    assert evaluated == [d.id]
    db.expire_all()
    assert db.get(Deployment, d.id).evaluation_status == "evaluated_no_regression"
    assert len(_evals(db, d.id)) == 1
    assert _incident_count(db) == 0


def test_due_degraded_deploy_opens_incident(db, engine) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    d = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=200, n_fail=0)
    db.commit()

    evaluated = evaluate_due_deployments(_factory(engine), now=NOW_DUE)

    assert evaluated == [d.id]
    db.expire_all()
    assert db.get(Deployment, d.id).evaluation_status == "evaluated_regression"
    assert _incident_count(db) == 1


def test_not_yet_due_is_skipped(db, engine) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    d = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
    db.commit()

    evaluated = evaluate_due_deployments(_factory(engine), now=NOW_EARLY)

    assert evaluated == []
    db.expire_all()
    assert db.get(Deployment, d.id).evaluation_status == "pending"
    assert _evals(db, d.id) == []


def test_already_evaluated_is_not_re_evaluated(db, engine) -> None:
    svc = _service(db)
    ep = _endpoint(db, svc.id)
    d = _deployment(db, svc.id)
    _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
    _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
    db.commit()

    assert evaluate_due_deployments(_factory(engine), now=NOW_DUE) == [d.id]
    # Second pass: nothing pending, so nothing is touched.
    assert evaluate_due_deployments(_factory(engine), now=NOW_DUE) == []
    db.expire_all()
    assert db.get(Deployment, d.id).evaluation_status == "evaluated_no_regression"
    assert len(_evals(db, d.id)) == 1


def test_multiple_due_deployments_all_evaluated(db, engine) -> None:
    ids = set()
    for i in (1, 2):
        svc = _service(db, name=f"svc{i}")
        ep = _endpoint(db, svc.id, url=f"https://svc{i}/api")
        d = _deployment(db, svc.id, external_id=f"e:{i}")
        _seed_window(db, ep.id, BASELINE_START, n_success=20, latency=100, n_fail=0)
        _seed_window(db, ep.id, OBS_START, n_success=20, latency=100, n_fail=0)
        ids.add(d.id)
    db.commit()

    evaluated = evaluate_due_deployments(_factory(engine), now=NOW_DUE)

    assert set(evaluated) == ids
