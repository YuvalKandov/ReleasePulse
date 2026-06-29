"""Worker probe tests: the pure readiness decision plus an HTTP smoke test.

evaluate_readiness is the real logic and is tested branch by branch with fakes,
so no scheduler, loop, or database is needed. The HTTP test only confirms the
app wires that decision to the right status codes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from releasepulse.worker.http import WorkerState, create_app, evaluate_readiness

MAX_AGE = timedelta(seconds=180)
NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


class _FakeSession:
    """Stands in for a SQLAlchemy Session used as a context manager."""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        if self._fail:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def _factory(*, fail: bool = False):
    return lambda: _FakeSession(fail=fail)


def _ready_state(*, heartbeat: datetime = NOW) -> WorkerState:
    return WorkerState(
        scheduler=SimpleNamespace(running=True),
        session_factory=_factory(),
        last_heartbeat=heartbeat,
    )


def test_ready_when_db_scheduler_and_heartbeat_all_good() -> None:
    ok, reason = evaluate_readiness(_ready_state(), now=NOW, max_age=MAX_AGE)
    assert (ok, reason) == (True, "ready")


def test_not_ready_before_initialised() -> None:
    ok, reason = evaluate_readiness(WorkerState(), now=NOW, max_age=MAX_AGE)
    assert ok is False
    assert reason == "not initialised"


def test_not_ready_when_database_unreachable() -> None:
    state = _ready_state()
    state.session_factory = _factory(fail=True)
    ok, reason = evaluate_readiness(state, now=NOW, max_age=MAX_AGE)
    assert ok is False
    assert reason == "database unreachable"


def test_not_ready_when_scheduler_not_running() -> None:
    state = _ready_state()
    state.scheduler = SimpleNamespace(running=False)
    ok, reason = evaluate_readiness(state, now=NOW, max_age=MAX_AGE)
    assert ok is False
    assert reason == "scheduler not started"


def test_not_ready_before_first_heartbeat() -> None:
    state = _ready_state()
    state.last_heartbeat = None
    ok, reason = evaluate_readiness(state, now=NOW, max_age=MAX_AGE)
    assert ok is False
    assert reason == "no heartbeat yet"


def test_not_ready_when_heartbeat_is_stale() -> None:
    state = _ready_state()
    state.last_heartbeat = NOW - (MAX_AGE + timedelta(seconds=1))
    ok, reason = evaluate_readiness(state, now=NOW, max_age=MAX_AGE)
    assert ok is False
    assert reason == "heartbeat stale"


def test_beat_records_a_recent_heartbeat() -> None:
    state = WorkerState()
    assert state.last_heartbeat is None
    state.beat()
    assert state.last_heartbeat is not None
    # Fresh against a now taken just after.
    assert datetime.now(timezone.utc) - state.last_heartbeat < timedelta(seconds=5)


def test_http_healthz_and_readyz_map_to_status_codes() -> None:
    # This goes through the real route, which uses wall-clock now(), so the
    # heartbeat must be anchored to real time rather than the fixed NOW the pure
    # evaluate_readiness tests use.
    app = create_app(
        _ready_state(heartbeat=datetime.now(timezone.utc)), heartbeat_max_age=MAX_AGE
    )
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_http_readyz_reports_503_with_reason_when_not_ready() -> None:
    state = _ready_state()
    state.scheduler = SimpleNamespace(running=False)
    client = TestClient(create_app(state, heartbeat_max_age=MAX_AGE))

    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not ready", "reason": "scheduler not started"}
