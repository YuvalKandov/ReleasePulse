"""HMAC verification for the deployment webhook (Phase 1).

The sender signs ``"<timestamp>.<raw_body>"`` with the shared WEBHOOK_SECRET and
sends three headers; the receiver recomputes the HMAC over the *exact* bytes it
received, constant-time-compares, and enforces a freshness window. Event-id
replay dedup is a database concern handled by the router, not here.

Kept free of FastAPI request plumbing (it takes a plain header mapping, the raw
body, and an injectable ``now``) so every rejection branch is unit-testable and
deterministic.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from fastapi import HTTPException, status

TIMESTAMP_HEADER = "X-Sentinel-Timestamp"
SIGNATURE_HEADER = "X-Sentinel-Signature"
EVENT_ID_HEADER = "X-Sentinel-Event-Id"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _signed_payload(timestamp: str, body: bytes) -> bytes:
    """The exact byte string both sides feed to HMAC: "<timestamp>." + raw body.

    The literal timestamp header string is used (not a re-parsed int) so the two
    sides can't disagree on formatting.
    """
    return timestamp.encode() + b"." + body


def expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    """The hex HMAC-SHA256 a correct sender would produce. Shared with tests."""
    return hmac.new(
        secret.encode(), _signed_payload(timestamp, body), hashlib.sha256
    ).hexdigest()


def verify_webhook_signature(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: str,
    now: int,
    max_age: int,
) -> str:
    """Verify the signed webhook. Return the event id, or raise HTTP 401.

    `now` and `max_age` are unix epoch seconds, injected so the freshness check is
    deterministic in tests.
    """
    timestamp = headers.get(TIMESTAMP_HEADER)
    signature = headers.get(SIGNATURE_HEADER)
    event_id = headers.get(EVENT_ID_HEADER)
    if not timestamp or not signature or not event_id:
        raise _unauthorized("missing signature headers")

    try:
        ts = int(timestamp)
    except ValueError:
        raise _unauthorized("invalid timestamp")

    # Reject both stale (replay) and far-future (skew/forgery) timestamps.
    if abs(now - ts) > max_age:
        raise _unauthorized("timestamp outside acceptance window")

    expected = expected_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise _unauthorized("signature mismatch")

    return event_id
