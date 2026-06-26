"""Shared pytest fixtures: a Postgres-backed, per-test-isolated database.

Runs against a dedicated TEST database (never the dev one). Create it once:

    docker exec -it releasepulse-pg psql -U postgres -c "CREATE DATABASE releasepulse_test;"

Override the URL with TEST_DATABASE_URL if your local credentials differ.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

import releasepulse.models  # noqa: F401  (import registers every table on Base.metadata)
from releasepulse.db import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:dev@localhost:5432/releasepulse_test",
)

# Truncated before each test for isolation. CASCADE + the order are belt-and-braces.
_ALL_TABLES = (
    "alerts",
    "incidents",
    "deployment_endpoint_evaluations",
    "checks",
    "deployments",
    "endpoints",
    "services",
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    # Start every test from an empty schema.
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
