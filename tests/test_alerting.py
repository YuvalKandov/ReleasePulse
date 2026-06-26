"""Tests for alert dispatch and the Telegram sender.

dispatch_pending_alerts is driven with asyncio.run (no async plugin in this project,
matching test_worker_check). DB-backed cases use a FakeAlertSender; the sender itself
is exercised against an httpx.MockTransport so no network is touched.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from releasepulse.alerting.dispatch import dispatch_pending_alerts
from releasepulse.alerting.sender import TelegramAlertSender
from releasepulse.models import Alert, Incident
from tests.test_detector_service import _deployment, _service

UTC = timezone.utc


# --- helpers --------------------------------------------------------------

class FakeAlertSender:
    """Records the incidents it was asked to send; optionally fails every send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list = []

    async def send_incident(self, incident: Incident) -> None:
        self.calls.append(incident.id)
        if self.fail:
            raise RuntimeError("telegram down")


def _incident(db, *, external_id, summary="boom regression", environment="production"):
    svc = _service(db, name=f"svc-{external_id}")
    dep = _deployment(db, svc.id, external_id=external_id)
    inc = Incident(
        service_id=svc.id,
        deployment_id=dep.id,
        environment=environment,
        detected_at=datetime.now(UTC),
        status="open",
        summary=summary,
    )
    db.add(inc)
    db.flush()
    return inc


def _alert(db, incident_id, channel="telegram") -> Alert | None:
    return db.scalar(
        select(Alert).where(Alert.incident_id == incident_id, Alert.channel == channel)
    )


def _alert_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Alert))


def _dispatch(db, sender, **kwargs) -> int:
    return asyncio.run(dispatch_pending_alerts(db, sender, **kwargs))


# --- dispatch -------------------------------------------------------------

def test_pending_incident_is_sent_and_tracked(db) -> None:
    inc = _incident(db, external_id="i:1")
    db.commit()

    sent = _dispatch(db, FakeAlertSender())

    assert sent == 1
    alert = _alert(db, inc.id)
    assert alert.status == "sent"
    assert alert.sent_at is not None
    assert alert.attempts == 1
    assert alert.last_error is None


def test_failed_send_is_recorded_then_retried(db) -> None:
    inc = _incident(db, external_id="i:1")
    db.commit()

    failing = FakeAlertSender(fail=True)
    assert _dispatch(db, failing) == 0
    assert failing.calls == [inc.id]
    alert = _alert(db, inc.id)
    assert alert.status == "failed"
    assert alert.attempts == 1
    assert alert.last_error

    # Next tick retries the same row and succeeds.
    assert _dispatch(db, FakeAlertSender()) == 1
    alert = _alert(db, inc.id)
    assert alert.status == "sent"
    assert alert.attempts == 2


def test_sent_incident_is_not_resent(db) -> None:
    inc = _incident(db, external_id="i:1")
    db.commit()
    assert _dispatch(db, FakeAlertSender()) == 1

    second = FakeAlertSender()
    assert _dispatch(db, second) == 0
    assert second.calls == []
    assert _alert_count(db) == 1


def test_failed_at_max_attempts_is_skipped(db) -> None:
    inc = _incident(db, external_id="i:1")
    db.add(
        Alert(incident_id=inc.id, channel="telegram", status="failed", attempts=5)
    )
    db.commit()

    sender = FakeAlertSender()
    assert _dispatch(db, sender, max_attempts=5) == 0
    assert sender.calls == []


def test_multiple_pending_incidents_all_sent(db) -> None:
    i1 = _incident(db, external_id="i:1")
    i2 = _incident(db, external_id="i:2")
    db.commit()

    sender = FakeAlertSender()
    assert _dispatch(db, sender) == 2
    assert set(sender.calls) == {i1.id, i2.id}
    assert _alert_count(db) == 2


# --- Telegram sender ------------------------------------------------------

def _bare_incident() -> Incident:
    return Incident(environment="production", summary="boom regression")


def test_telegram_sender_posts_sendmessage() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await TelegramAlertSender(client, "T0KEN", "12345").send_incident(
                _bare_incident()
            )

    asyncio.run(go())

    assert captured["url"] == "https://api.telegram.org/botT0KEN/sendMessage"
    assert captured["body"]["chat_id"] == "12345"
    assert "boom regression" in captured["body"]["text"]


def test_telegram_sender_raises_on_http_error() -> None:
    async def go() -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as client:
            await TelegramAlertSender(client, "T0KEN", "12345").send_incident(
                _bare_incident()
            )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())
