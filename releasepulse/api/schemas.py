"""Pydantic request/response schemas for the registration API.

These describe the JSON contract at the edge and are deliberately separate from
the SQLAlchemy models (database rows). Read schemas use from_attributes so they
can serialize directly from a model instance.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# --- services -------------------------------------------------------------

class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ServiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    created_at: datetime


# --- endpoints ------------------------------------------------------------

class EndpointCreate(BaseModel):
    url: str = Field(min_length=1)
    environment: str = "production"
    method: str = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    check_interval_sec: int = Field(default=30, ge=10)
    timeout_sec: int = Field(default=10, gt=0)
    enabled: bool = True
    # Optional per-endpoint detector overrides (NULL = fall back to defaults).
    latency_pct: float | None = Field(default=None, gt=0)
    latency_floor_ms: int | None = Field(default=None, ge=0)
    error_delta: float | None = Field(default=None, ge=0, le=1)


class EndpointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    environment: str
    url: str
    method: str
    expected_status: int
    check_interval_sec: int
    timeout_sec: int
    enabled: bool
    deleted_at: datetime | None
    latency_pct: float | None
    latency_floor_ms: int | None
    error_delta: float | None
    created_at: datetime


class EndpointUpdate(BaseModel):
    """PATCH body: enable/disable, interval, and threshold overrides.

    URL, method, and environment are immutable here - they form the endpoint's
    identity and changing the URL would require re-running SSRF validation.
    Every field is optional; only those sent are applied.
    """

    enabled: bool | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    check_interval_sec: int | None = Field(default=None, ge=10)
    timeout_sec: int | None = Field(default=None, gt=0)
    latency_pct: float | None = Field(default=None, gt=0)
    latency_floor_ms: int | None = Field(default=None, ge=0)
    error_delta: float | None = Field(default=None, ge=0, le=1)
