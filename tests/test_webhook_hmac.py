"""HMAC webhook auth (Phase 1): verifier unit tests + handler integration.

The verifier is tested branch by branch with an injected `now` (deterministic).
The handler tests drive the real endpoint through TestClient with the test
database, posting the *exact bytes that were signed* via content= (json= would
re-serialize and break the signature).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from releasepulse.api.deps import get_db
from releasepulse.api.main import app as api_app
from releasepulse.api.webhook_auth import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    expected_signature,
    verify_webhook_signature,
)
from releasepulse.config import Settings, get_settings
from releasepulse.models import Deployment, Service, WebhookDelivery

SECRET = "test-webhook-secret"
NOW = 1_700_000_000  # fixed epoch for deterministic verifier tests


# --- verifier unit tests --------------------------------------------------


def _headers(body: bytes, *, secret: str = SECRET, ts: int = NOW, event_id: str = "evt-1"):
    tss = str(ts)
    return {
        TIMESTAMP_HEADER: tss,
        SIGNATURE_HEADER: expected_signature(secret, tss, body),
        EVENT_ID_HEADER: event_id,
    }


def test_valid_signature_returns_event_id() -> None:
    body = b'{"service":"x"}'
    event_id = verify_webhook_signature(
        _headers(body), body, secret=SECRET, now=NOW, max_age=300
    )
    assert event_id == "evt-1"


def test_missing_headers_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature({}, b"{}", secret=SECRET, now=NOW, max_age=300)
    assert exc.value.status_code == 401


def test_wrong_secret_rejected() -> None:
    body = b'{"a":1}'
    headers = _headers(body, secret="other-secret")
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(headers, body, secret=SECRET, now=NOW, max_age=300)
    assert exc.value.status_code == 401


def test_tampered_body_rejected() -> None:
    headers = _headers(b'{"amount":1}')  # signed over the original
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(
            headers, b'{"amount":999}', secret=SECRET, now=NOW, max_age=300
        )
    assert exc.value.status_code == 401


def test_stale_timestamp_rejected() -> None:
    body = b"{}"
    headers = _headers(body, ts=NOW - 301)
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(headers, body, secret=SECRET, now=NOW, max_age=300)
    assert exc.value.status_code == 401


def test_future_timestamp_rejected() -> None:
    body = b"{}"
    headers = _headers(body, ts=NOW + 301)
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(headers, body, secret=SECRET, now=NOW, max_age=300)
    assert exc.value.status_code == 401


def test_non_integer_timestamp_rejected() -> None:
    body = b"{}"
    headers = _headers(body)
    headers[TIMESTAMP_HEADER] = "not-a-number"
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(headers, body, secret=SECRET, now=NOW, max_age=300)
    assert exc.value.status_code == 401


# --- handler integration --------------------------------------------------


def _client(db: Session) -> TestClient:
    def _override_db() -> Iterator[Session]:
        yield db

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_settings] = lambda: Settings(
        admin_token="test-admin-token", webhook_secret=SECRET
    )
    return TestClient(api_app)


def _sign_now(body: bytes, *, event_id: str) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: expected_signature(SECRET, ts, body),
        EVENT_ID_HEADER: event_id,
        "Content-Type": "application/json",
    }


def _payload(external_id: str = "deploy-1") -> dict:
    return {"service": "svc", "source": "manual", "external_id": external_id}


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _seed_service(db: Session) -> None:
    db.add(Service(name="svc"))
    db.commit()


def test_valid_webhook_creates_deployment_and_delivery(db: Session) -> None:
    _seed_service(db)
    client = _client(db)
    try:
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/webhooks/deployments", content=body, headers=_sign_now(body, event_id="e1")
        )
        assert resp.status_code == 201, resp.text
        assert _count(db, Deployment) == 1
        assert _count(db, WebhookDelivery) == 1
    finally:
        api_app.dependency_overrides.clear()


def test_replayed_event_id_is_idempotent(db: Session) -> None:
    _seed_service(db)
    client = _client(db)
    try:
        body = json.dumps(_payload()).encode()
        headers = _sign_now(body, event_id="e1")
        first = client.post("/webhooks/deployments", content=body, headers=headers)
        assert first.status_code == 201
        # The exact same signed delivery again - replay, must be a no-op.
        second = client.post("/webhooks/deployments", content=body, headers=headers)
        assert second.status_code == 200
        assert _count(db, Deployment) == 1
        assert _count(db, WebhookDelivery) == 1
    finally:
        api_app.dependency_overrides.clear()


def test_new_delivery_same_deployment_dedupes(db: Session) -> None:
    _seed_service(db)
    client = _client(db)
    try:
        body = json.dumps(_payload()).encode()
        first = client.post(
            "/webhooks/deployments", content=body, headers=_sign_now(body, event_id="e1")
        )
        assert first.status_code == 201
        # CI rerun: fresh signed delivery (new event id), same external_id.
        second = client.post(
            "/webhooks/deployments", content=body, headers=_sign_now(body, event_id="e2")
        )
        assert second.status_code == 200
        assert _count(db, Deployment) == 1  # no duplicate deployment
        assert _count(db, WebhookDelivery) == 2  # but both deliveries logged
    finally:
        api_app.dependency_overrides.clear()


def test_tampered_body_is_rejected_and_writes_nothing(db: Session) -> None:
    _seed_service(db)
    client = _client(db)
    try:
        signed_body = json.dumps(_payload()).encode()
        headers = _sign_now(signed_body, event_id="e1")
        tampered = json.dumps(_payload(external_id="evil")).encode()
        resp = client.post("/webhooks/deployments", content=tampered, headers=headers)
        assert resp.status_code == 401
        assert _count(db, Deployment) == 0
        assert _count(db, WebhookDelivery) == 0
    finally:
        api_app.dependency_overrides.clear()


def test_missing_signature_headers_rejected(db: Session) -> None:
    _seed_service(db)
    client = _client(db)
    try:
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/webhooks/deployments",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401
        assert _count(db, Deployment) == 0
    finally:
        api_app.dependency_overrides.clear()


def test_unknown_service_returns_404(db: Session) -> None:
    # No service seeded.
    client = _client(db)
    try:
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/webhooks/deployments", content=body, headers=_sign_now(body, event_id="e1")
        )
        assert resp.status_code == 404
        assert _count(db, WebhookDelivery) == 0
    finally:
        api_app.dependency_overrides.clear()
