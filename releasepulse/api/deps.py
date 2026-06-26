"""Shared FastAPI dependencies: per-request DB session and admin auth."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from releasepulse.config import Settings, get_settings
from releasepulse.db import get_sessionmaker

# auto_error=False so a missing token reaches us as None and we return our own
# 401 (rather than HTTPBearer's default 403). The scheme also makes the /docs
# "Authorize" button appear.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_db() -> Iterator[Session]:
    """Yield one Session per request, always closed afterwards."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject any request lacking a valid `Authorization: Bearer <ADMIN_TOKEN>`.

    Uses a constant-time comparison so the check doesn't leak the token via
    response timing.
    """
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, settings.admin_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_webhook_secret(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject any webhook lacking a valid `Authorization: Bearer <WEBHOOK_SECRET>`.

    Separate from require_admin: CI/CD holds the webhook secret, not the admin token.
    """
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, settings.webhook_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid webhook secret",
            headers={"WWW-Authenticate": "Bearer"},
        )
