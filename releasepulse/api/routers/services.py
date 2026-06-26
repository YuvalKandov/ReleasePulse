"""Service registration endpoints (admin-only)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from releasepulse.api import schemas
from releasepulse.api.deps import get_db, require_admin
from releasepulse.models import Service

router = APIRouter(
    prefix="/services",
    tags=["services"],
    dependencies=[Depends(require_admin)],
)


@router.post("", response_model=schemas.ServiceRead, status_code=status.HTTP_201_CREATED)
def create_service(payload: schemas.ServiceCreate, db: Session = Depends(get_db)):
    service = Service(name=payload.name)
    db.add(service)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"service '{payload.name}' already exists",
        )
    db.refresh(service)
    return service


@router.get("", response_model=list[schemas.ServiceRead])
def list_services(db: Session = Depends(get_db)):
    return db.scalars(select(Service).order_by(Service.created_at)).all()


@router.get("/{service_id}", response_model=schemas.ServiceRead)
def get_service(service_id: UUID, db: Session = Depends(get_db)):
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")
    return service
