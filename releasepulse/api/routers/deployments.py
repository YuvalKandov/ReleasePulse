"""Deployment ingestion (webhook) and listing endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from releasepulse.api import schemas
from releasepulse.api.deps import get_db, require_admin, require_webhook_secret
from releasepulse.ingestion import compute_effective_deployed_at
from releasepulse.models import Deployment, Service

router = APIRouter(tags=["deployments"])


def _find_deployment(db: Session, source: str, external_id: str) -> Deployment | None:
    return db.scalar(
        select(Deployment).where(
            Deployment.source == source,
            Deployment.external_id == external_id,
        )
    )


@router.post(
    "/webhooks/deployments",
    response_model=schemas.DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_webhook_secret)],
)
def ingest_deployment(
    payload: schemas.DeploymentCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    service = db.scalar(select(Service).where(Service.name == payload.service))
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown service '{payload.service}'",
        )

    # Idempotency: a duplicate (source, external_id) returns the existing row
    # with 200 and never inserts again.
    existing = _find_deployment(db, payload.source, payload.external_id)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    received = datetime.now(timezone.utc)
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
        db.commit()
    except IntegrityError:
        # Race: a concurrent identical delivery won the insert. Return that one.
        db.rollback()
        existing = _find_deployment(db, payload.source, payload.external_id)
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return existing
    db.refresh(deployment)
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
