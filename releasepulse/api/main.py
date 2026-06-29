"""FastAPI application entrypoint for ReleasePulse.

Phase 0A exposes only the admin-authenticated registration API. The webhook,
detector, and worker are added in later steps.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Response
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from releasepulse.api.deps import get_db
from releasepulse.api.routers import deployments, endpoints, services
from releasepulse.metrics import CONTENT_TYPE_LATEST, generate_latest

app = FastAPI(title="ReleasePulse", version="0.1.0")

app.include_router(services.router)
app.include_router(endpoints.router)
app.include_router(deployments.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness probe: the process is up and serving.

    Deliberately dumb - it must not touch the database. Kubernetes restarts the
    pod when liveness fails, and a transient Postgres blip should never trigger a
    restart storm of otherwise-healthy API pods. "Can I serve a request?" lives
    in /readyz instead.
    """
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz(response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness probe: can this pod actually serve traffic right now?

    Checks the two things an API request needs: the database is reachable, and
    the schema has been migrated. When it fails, Kubernetes pulls the pod out of
    the Service load-balancer (but leaves it running), and routes back to it the
    moment this returns 200 again.

    Uses the same injected session the rest of the API uses, so it exercises the
    real connection pool (and tests can point it at their database through the
    usual get_db override).

    We confirm migrations by reading Alembic's `alembic_version` row rather than
    comparing it to the latest revision in this code: an exact-head match would
    make pods flap out of readiness during a rolling upgrade, when the migrate
    job has already advanced the DB past the revision an older pod still carries.
    "Has the DB been migrated at all" is the stable, honest signal.
    """
    try:
        db.execute(text("SELECT 1"))
    except OperationalError:
        # Can't even talk to Postgres (down, wrong host, auth, network).
        response.status_code = 503
        return {"status": "not ready", "reason": "database unreachable"}

    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except ProgrammingError:
        # DB is up but the alembic_version table doesn't exist - the migrate job
        # has not run yet. The failed query aborted this transaction, so roll it
        # back before the session is returned to the pool.
        db.rollback()
        response.status_code = 503
        return {"status": "not ready", "reason": "migrations not applied"}

    if not revision:
        response.status_code = 503
        return {"status": "not ready", "reason": "migrations not applied"}

    return {"status": "ready"}


@app.get("/metrics", tags=["ops"])
def metrics() -> Response:
    """Prometheus scrape endpoint for the API process's self-metrics.

    Returns the default registry in the text exposition format. This process only
    populates webhook counters; the worker exposes the check/detector/alert ones
    on its own /metrics. Prometheus scrapes both and sums across.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
