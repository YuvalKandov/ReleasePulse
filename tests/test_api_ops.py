"""Liveness and readiness probe tests for the API (Phase 1).

/healthz must stay dependency-free (it only proves the process is up). /readyz
checks the database is reachable and the schema has been migrated, and reports a
precise reason for each not-ready case so an operator can tell "DB down" from
"migrate job hasn't run".
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from releasepulse.api.deps import get_db
from releasepulse.api.main import app


def _client_with_db(session: Session) -> TestClient:
    # A generator *function* (not a lambda returning an iterator): FastAPI detects
    # generator dependencies by inspecting the callable, mirroring test_e2e.py.
    def _override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def test_healthz_is_always_ok() -> None:
    # Liveness takes no DB dependency, so it answers even with no override set.
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ready_when_db_up_and_migrated(db: Session) -> None:
    # Simulate a migrated database: the migrate job leaves an alembic_version row.
    db.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)"))
    db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('e96f69f637c4')"))
    db.commit()
    try:
        client = _client_with_db(db)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}
    finally:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
        db.commit()
        app.dependency_overrides.clear()


def test_readyz_not_ready_when_not_migrated(db: Session) -> None:
    # The test schema is built with create_all, so alembic_version never exists -
    # exactly the state of a DB the migrate job has not touched yet.
    try:
        client = _client_with_db(db)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "migrations not applied"
    finally:
        app.dependency_overrides.clear()


def test_readyz_not_ready_when_db_unreachable() -> None:
    class _BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    def _broken_db() -> Iterator[_BrokenSession]:
        yield _BrokenSession()

    app.dependency_overrides[get_db] = _broken_db
    try:
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "database unreachable"
    finally:
        app.dependency_overrides.clear()
