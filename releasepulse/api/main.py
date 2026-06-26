"""FastAPI application entrypoint for ReleasePulse.

Phase 0A exposes only the admin-authenticated registration API. The webhook,
detector, and worker are added in later steps.
"""

from __future__ import annotations

from fastapi import FastAPI

from releasepulse.api.routers import endpoints, services

app = FastAPI(title="ReleasePulse", version="0.1.0")

app.include_router(services.router)
app.include_router(endpoints.router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness probe - the process is up and serving. (Readiness comes later.)"""
    return {"status": "ok"}
