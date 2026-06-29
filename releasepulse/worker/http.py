"""The worker's internal HTTP server: liveness and readiness probes.

The check worker has no public API, but Kubernetes still needs to probe it. We
run a tiny ASGI app on an internal port, inside the *same* asyncio loop as the
scheduler, so the probes observe the real worker state rather than a guess.

The readiness decision lives in a pure function (`evaluate_readiness`) so every
not-ready branch is unit-testable without standing up a server. The /metrics
route is added in the next step (Prometheus self-metrics).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, Response
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from releasepulse.metrics import CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    """Shared, mutable view of the worker that the probes read.

    main() creates one instance and hands the *same* object to both the heartbeat
    updater and the probe app, so /readyz always reflects the live scheduler and
    the latest heartbeat instead of a stale snapshot.
    """

    scheduler: object | None = None
    session_factory: sessionmaker[Session] | None = None
    last_heartbeat: datetime | None = None

    def beat(self) -> None:
        """Record that the scheduler loop just fired a job. Called by reconcile."""
        self.last_heartbeat = datetime.now(timezone.utc)


def evaluate_readiness(
    state: WorkerState, *, now: datetime, max_age: timedelta
) -> tuple[bool, str]:
    """Decide whether the worker is ready, separated from HTTP for testability.

    Ready requires all three (spec ~12):
    - the database answers (the worker is useless if it can't read/write checks),
    - the scheduler is running, and
    - a heartbeat landed recently - proof the loop is actually firing jobs, not
      merely flagged 'started' while wedged.

    Returns (ready, reason); the reason is surfaced on the 503 so an operator can
    tell "DB down" from "scheduler wedged".
    """
    if state.session_factory is None:
        return False, "not initialised"
    try:
        with state.session_factory() as session:
            session.execute(text("SELECT 1"))
    except OperationalError:
        return False, "database unreachable"

    scheduler = state.scheduler
    if scheduler is None or not getattr(scheduler, "running", False):
        return False, "scheduler not started"

    if state.last_heartbeat is None:
        return False, "no heartbeat yet"
    if now - state.last_heartbeat > max_age:
        return False, "heartbeat stale"

    return True, "ready"


def create_app(state: WorkerState, *, heartbeat_max_age: timedelta) -> FastAPI:
    """Build the worker's probe app over the given shared state."""
    app = FastAPI(title="ReleasePulse worker", version="0.1.0")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Liveness: the loop is turning enough to answer. No dependency checks."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def readyz(response: Response) -> dict[str, str]:
        ok, reason = evaluate_readiness(
            state, now=datetime.now(timezone.utc), max_age=heartbeat_max_age
        )
        if not ok:
            response.status_code = 503
            return {"status": "not ready", "reason": reason}
        return {"status": "ready"}

    @app.get("/metrics", tags=["ops"])
    def metrics() -> Response:
        """Prometheus scrape endpoint for the worker process's self-metrics
        (checks, detector evaluations, alert deliveries)."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


class _NoSignalServer(uvicorn.Server):
    """uvicorn installs SIGINT/SIGTERM handlers in serve(); embedded inside the
    worker's own loop that would hijack the worker's shutdown. The worker owns the
    process lifecycle, so we make uvicorn's signal handling a no-op."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - trivial override
        return


def build_server(app: FastAPI, *, host: str, port: int) -> uvicorn.Server:
    """A uvicorn server suitable for running as a task in an existing loop."""
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    return _NoSignalServer(config)
