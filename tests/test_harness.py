"""Self-test for the db fixture: persistence works and tests are isolated."""

from __future__ import annotations

from releasepulse.models import Service


def test_db_fixture_persists_and_reads(db) -> None:
    db.add(Service(name="svc-a"))
    db.commit()
    assert [s.name for s in db.query(Service).all()] == ["svc-a"]


def test_db_fixture_is_isolated_between_tests(db) -> None:
    # Reusing the same unique name only works if the previous test's row was
    # truncated away - so this passing proves isolation.
    db.add(Service(name="svc-a"))
    db.commit()
    assert db.query(Service).count() == 1
