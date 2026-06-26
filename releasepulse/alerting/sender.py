"""The alert channel behind a single interface.

One provider for the MVP (Telegram). Everything that dispatches alerts depends on
the AlertSender Protocol, not on Telegram, so the e2e test can inject a fake and a
second provider would be additive. send_incident raises on delivery failure; the
dispatcher turns that into a tracked retry.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from releasepulse.models import Incident


class AlertSender(Protocol):
    async def send_incident(self, incident: Incident) -> None: ...


def _format_message(incident: Incident) -> str:
    summary = incident.summary or "regression detected"
    return f"\U0001f6a8 ReleasePulse [{incident.environment}]\n{summary}"


class TelegramAlertSender:
    """Sends one Telegram message per incident via the Bot API sendMessage method."""

    def __init__(self, client: httpx.AsyncClient, bot_token: str, chat_id: str) -> None:
        self._client = client
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send_incident(self, incident: Incident) -> None:
        response = await self._client.post(
            self._url,
            json={"chat_id": self._chat_id, "text": _format_message(incident)},
        )
        response.raise_for_status()
