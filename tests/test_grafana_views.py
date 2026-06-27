"""Tests for the Grafana read-layer views (deploy/grafana/views.sql).

The dashboard is only as trustworthy as these views, so we apply the real SQL file
to the test database and assert the views expose the right denormalized rows. This
is what keeps the dashboard's data layer verified rather than just eyeballed.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from tests.test_detector_service import (
    BASELINE_START,
    _deployment,
    _endpoint,
    _seed_window,
    _service,
)

VIEWS_SQL = Path(__file__).resolve().parent.parent / "deploy" / "grafana" / "views.sql"


def _apply_views(engine) -> None:
    sql = VIEWS_SQL.read_text()
    with engine.begin() as conn:
        for statement in sql.split(";"):
            if statement.strip():
                conn.execute(text(statement))


def test_service_check_metrics_view_denormalizes_checks(db, engine) -> None:
    svc = _service(db, name="grafana-svc")
    ep = _endpoint(db, svc.id, url="https://grafana-svc/api")
    _seed_window(db, ep.id, BASELINE_START, n_success=3, latency=120, n_fail=1)
    db.commit()
    _apply_views(engine)

    rows = db.execute(
        text(
            "SELECT service_name, endpoint_url, environment, success, latency_ms "
            "FROM service_check_metrics_view"
        )
    ).all()

    assert len(rows) == 4  # 3 success + 1 failure
    assert {r.service_name for r in rows} == {"grafana-svc"}
    assert {r.endpoint_url for r in rows} == {"https://grafana-svc/api"}
    assert {r.environment for r in rows} == {"production"}
    assert sorted(r.latency_ms for r in rows if r.success) == [120, 120, 120]
    assert [r.latency_ms for r in rows if not r.success] == [None]


def test_deployment_annotations_view_one_row_per_deploy(db, engine) -> None:
    svc = _service(db, name="grafana-svc")
    _deployment(db, svc.id, external_id="gh:1")
    db.commit()
    _apply_views(engine)

    rows = db.execute(
        text(
            "SELECT service_name, environment, release, evaluation_status "
            "FROM deployment_annotations_view"
        )
    ).all()

    assert len(rows) == 1
    assert rows[0].service_name == "grafana-svc"
    assert rows[0].environment == "production"
    assert rows[0].evaluation_status == "pending"
    # version and commit_sha are unset, so release falls back via COALESCE.
    assert rows[0].release == "unknown"
