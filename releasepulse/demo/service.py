"""A configurable demo target for ReleasePulse to monitor.

Stands in for a real deployed app: a single checked endpoint whose latency and
error rate are tunable at runtime via POST /admin/mode. This is what lets the
end-to-end test (and a manual Compose smoke) drive a clean baseline, then a
deploy event, then a deliberate degradation, and watch the platform catch it.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class Mode(BaseModel):
    latency_ms: int = Field(default=0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)


def create_app() -> FastAPI:
    app = FastAPI(title="ReleasePulse demo service", version="0.1.0")
    app.state.mode = Mode()

    @app.get("/")
    async def root() -> JSONResponse:
        mode: Mode = app.state.mode
        if mode.latency_ms:
            await asyncio.sleep(mode.latency_ms / 1000)
        if mode.error_rate and random.random() < mode.error_rate:
            return JSONResponse(status_code=500, content={"status": "error"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.post("/admin/mode")
    async def set_mode(mode: Mode) -> Mode:
        app.state.mode = mode
        return mode

    return app


app = create_app()
