"""Endpoint registration endpoints (admin-only), with SSRF validation."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from releasepulse.api import schemas
from releasepulse.api.deps import get_db, require_admin
from releasepulse.config import Settings, get_settings
from releasepulse.models import Endpoint, Service
from releasepulse.security.ssrf import SsrfValidationError, validate_url

router = APIRouter(tags=["endpoints"], dependencies=[Depends(require_admin)])


@router.post(
    "/services/{service_id}/endpoints",
    response_model=schemas.EndpointRead,
    status_code=status.HTTP_201_CREATED,
)
def create_endpoint(
    service_id: UUID,
    payload: schemas.EndpointCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if db.get(Service, service_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")

    # SSRF guard: reject URLs that resolve to non-routable addresses.
    try:
        validate_url(
            payload.url,
            app_env=settings.app_env,
            allowlist_raw=settings.ssrf_allowlist,
        )
    except SsrfValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL rejected: {exc}",
        )

    endpoint = Endpoint(service_id=service_id, **payload.model_dump())
    db.add(endpoint)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an endpoint with this environment, url, and method already exists",
        )
    db.refresh(endpoint)
    return endpoint


@router.get(
    "/services/{service_id}/endpoints",
    response_model=list[schemas.EndpointRead],
)
def list_endpoints(service_id: UUID, db: Session = Depends(get_db)):
    if db.get(Service, service_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")
    stmt = (
        select(Endpoint)
        .where(Endpoint.service_id == service_id, Endpoint.deleted_at.is_(None))
        .order_by(Endpoint.created_at)
    )
    return db.scalars(stmt).all()


def _get_live_endpoint(endpoint_id: UUID, db: Session) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None or endpoint.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    return endpoint


@router.patch("/endpoints/{endpoint_id}", response_model=schemas.EndpointRead)
def update_endpoint(
    endpoint_id: UUID,
    payload: schemas.EndpointUpdate,
    db: Session = Depends(get_db),
):
    endpoint = _get_live_endpoint(endpoint_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(endpoint, field, value)
    db.commit()
    db.refresh(endpoint)
    return endpoint


@router.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_endpoint(endpoint_id: UUID, db: Session = Depends(get_db)):
    endpoint = _get_live_endpoint(endpoint_id, db)
    endpoint.deleted_at = datetime.now(timezone.utc)  # soft delete; history retained
    db.commit()
