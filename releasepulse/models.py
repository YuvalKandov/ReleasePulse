"""SQLAlchemy models for the Phase 0A schema (spec section 8).

Seven tables: services, endpoints, checks, deployments,
deployment_endpoint_evaluations, incidents, alerts.

Conventions:
- UUID primary keys (Python-side `uuid4` default) for externally exposed rows;
  `checks.id` is bigserial because that table grows fastest.
- All timestamps are timezone-aware (timestamptz).
- Controlled vocabularies are plain text columns guarded by CHECK constraints,
  so adding a value later is a one-line migration rather than an ALTER TYPE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from releasepulse.db import Base


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="production"
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False, server_default="GET")
    expected_status: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("200")
    )
    check_interval_sec: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )
    timeout_sec: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-endpoint detector overrides; NULL means "fall back to the default".
    latency_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    latency_floor_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_delta: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "environment",
            "url",
            "method",
            name="uq_endpoints_service_env_url_method",
        ),
        CheckConstraint("check_interval_sec >= 10", name="check_interval_min"),
        CheckConstraint("timeout_sec > 0", name="timeout_positive"),
        CheckConstraint(
            "expected_status BETWEEN 100 AND 599", name="expected_status_range"
        ),
    )


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id"), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_checks_endpoint_id_checked_at", "endpoint_id", "checked_at"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"
        ),
        CheckConstraint(
            "error_type IS NULL OR error_type IN "
            "('timeout','dns_error','connection_refused',"
            "'tls_error','unexpected_status','internal_error')",
            name="error_type_allowed",
        ),
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    environment: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="production"
    )
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    reported_deployed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Drives evaluation - the single timestamp the detector keys off.
    effective_deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # "metadata" is reserved on the declarative Base, so the Python attribute is
    # `meta` while the DB column keeps the spec name "metadata".
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    evaluation_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    evaluation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_deployments_source_external_id"),
        Index(
            "ix_deployments_service_env_effective",
            "service_id",
            "environment",
            "effective_deployed_at",
        ),
        CheckConstraint(
            "source IN ('github-actions','argocd','manual')", name="source_allowed"
        ),
        CheckConstraint(
            "evaluation_status IN ('pending','evaluated_no_regression',"
            "'evaluated_regression','insufficient_baseline','superseded','invalid')",
            name="evaluation_status_allowed",
        ),
    )


class DeploymentEndpointEvaluation(Base):
    __tablename__ = "deployment_endpoint_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=False
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_median_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    observed_median_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    baseline_error_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    observed_error_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_failed_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "endpoint_id", name="uq_dee_deployment_endpoint"
        ),
        CheckConstraint(
            "outcome IN ('regressed_latency','regressed_error','regressed_both',"
            "'no_regression','insufficient_baseline','insufficient_observation',"
            "'insufficient_successful_baseline','baseline_degraded','superseded')",
            name="outcome_allowed",
        ),
        CheckConstraint(
            "baseline_error_rate BETWEEN 0 AND 1", name="baseline_error_rate_range"
        ),
        CheckConstraint(
            "observed_error_rate BETWEEN 0 AND 1", name="observed_error_rate_range"
        ),
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=False, unique=True
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="open"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','acknowledged','resolved')", name="status_allowed"
        ),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("incident_id", "channel", name="uq_alerts_incident_channel"),
        CheckConstraint(
            "status IN ('pending','sent','failed')", name="status_allowed"
        ),
    )
