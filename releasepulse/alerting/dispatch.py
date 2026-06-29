"""Outbox-style alert dispatch.

The detector writes Incident rows; it never sends anything. This scans for incidents
that still need an alert on a channel and drives the AlertSender, recording delivery in
the alerts table. A failed send is just a row to revisit next tick, which is where retry
comes from for free. Dual-write caveat (spec 8): the alerts row is written before the
HTTP call, so a crash in between can resend on retry - acceptable for the MVP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from releasepulse.alerting.sender import AlertSender
from releasepulse.metrics import alerts_total
from releasepulse.models import Alert, Incident

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5


async def dispatch_pending_alerts(
    session: Session,
    sender: AlertSender,
    *,
    channel: str = "telegram",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Send an alert for every incident that still needs one. Returns the count sent."""
    rows = session.execute(
        select(Incident, Alert)
        .outerjoin(
            Alert,
            and_(Alert.incident_id == Incident.id, Alert.channel == channel),
        )
        .where(
            or_(
                Alert.id.is_(None),
                and_(
                    Alert.status.in_(("pending", "failed")),
                    Alert.attempts < max_attempts,
                ),
            )
        )
    ).all()

    sent = 0
    for incident, alert in rows:
        if alert is None:
            alert = Alert(
                incident_id=incident.id, channel=channel, status="pending", attempts=0
            )
            session.add(alert)
        # Record the attempt before the call so the row survives a crash mid-send.
        alert.attempts = (alert.attempts or 0) + 1
        alert.status = "pending"
        session.commit()

        try:
            await sender.send_incident(incident)
        except Exception as exc:  # delivery failed; leave it retryable for next tick
            alert.status = "failed"
            alert.last_error = str(exc)[:1000]
            session.commit()
            alerts_total.labels(result="failed").inc()
            logger.warning("alert delivery failed for incident %s: %s", incident.id, exc)
            continue

        alert.status = "sent"
        alert.sent_at = datetime.now(timezone.utc)
        alert.last_error = None
        session.commit()
        alerts_total.labels(result="sent").inc()
        sent += 1

    return sent
