"""Deployment ingestion (webhook) and listing endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from releasepulse.api import schemas
from releasepulse.api.deps import get_db, require_admin
from releasepulse.api.webhook_auth import verify_webhook_signature
from releasepulse.config import Settings, get_settings
from releasepulse.ingestion import compute_effective_deployed_at
from releasepulse.metrics import webhooks_total
from releasepulse.models import Deployment, Service, WebhookDelivery

router = APIRouter(tags=["deployments"])


def _find_deployment(db: Session, source: str, external_id: str) -> Deployment | None:
    return db.scalar(
        select(Deployment).where(
            Deployment.source == source,
            Deployment.external_id == external_id,
        )
    )


def _find_delivery(db: Session, event_id: str) -> WebhookDelivery | None:
    return db.scalar(
        select(WebhookDelivery).where(WebhookDelivery.event_id == event_id)
    )


@router.post(
    "/webhooks/deployments",
    response_model=schemas.DeploymentRead,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_deployment(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # HMAC is computed over the exact bytes received, so read the raw body before
    # any parsing. now is epoch seconds, injected into the verifier so its
    # freshness window is deterministic.
    raw = await request.body()
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        event_id = verify_webhook_signature(
            request.headers,
            raw,
            secret=settings.webhook_secret,
            now=now,
            max_age=settings.webhook_hmac_window_sec,
        )
    except HTTPException:
        webhooks_total.labels(result="rejected").inc()
        raise

    # Bytes are authenticated; now they can be trusted enough to parse.
    try:
        payload = schemas.DeploymentCreate.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ],
        )

    service = db.scalar(select(Service).where(Service.name == payload.service))
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown service '{payload.service}'",
        )

    # Replay guard: this exact signed delivery was already accepted. Idempotent
    # no-op - return the deployment it pertained to.
    existing_delivery = _find_delivery(db, event_id)
    if existing_delivery is not None:
        webhooks_total.labels(result="duplicate").inc()
        response.status_code = status.HTTP_200_OK
        return db.get(Deployment, existing_delivery.deployment_id)

    # A new delivery for an already-known deployment (e.g. a CI rerun signing a
    # fresh request) reuses the deployment and only logs the new delivery.
    received = datetime.now(timezone.utc)
    existing = _find_deployment(db, payload.source, payload.external_id)
    created = existing is None
    if existing is not None:
        deployment = existing
    else:
        deployment = Deployment(
            service_id=service.id,
            environment=payload.environment,
            version=payload.version,
            commit_sha=payload.commit_sha,
            source=payload.source,
            external_id=payload.external_id,
            reported_deployed_at=payload.reported_deployed_at,
            received_at=received,
            effective_deployed_at=compute_effective_deployed_at(
                payload.reported_deployed_at, received
            ),
            meta=payload.metadata,
        )
        db.add(deployment)

    try:
        if created:
            db.flush()  # assign deployment.id for the delivery FK
        db.add(
            WebhookDelivery(
                event_id=event_id, deployment_id=deployment.id, received_at=received
            )
        )
        db.commit()
    except IntegrityError:
        # Race: a concurrent delivery won on event_id or (source, external_id).
        db.rollback()
        existing_delivery = _find_delivery(db, event_id)
        if existing_delivery is not None:
            webhooks_total.labels(result="duplicate").inc()
            response.status_code = status.HTTP_200_OK
            return db.get(Deployment, existing_delivery.deployment_id)
        existing = _find_deployment(db, payload.source, payload.external_id)
        if existing is None:
            raise
        webhooks_total.labels(result="duplicate").inc()
        response.status_code = status.HTTP_200_OK
        return existing

    db.refresh(deployment)
    if created:
        webhooks_total.labels(result="received").inc()
        return deployment
    webhooks_total.labels(result="duplicate").inc()
    response.status_code = status.HTTP_200_OK
    return deployment


@router.get(
    "/services/{service_id}/deployments",
    response_model=list[schemas.DeploymentRead],
    dependencies=[Depends(require_admin)],
)
def list_deployments(service_id: UUID, db: Session = Depends(get_db)):
    if db.get(Service, service_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")
    stmt = (
        select(Deployment)
        .where(Deployment.service_id == service_id)
        .order_by(Deployment.effective_deployed_at.desc())
    )
    return db.scalars(stmt).all()
