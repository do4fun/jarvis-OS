# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Canal de messagerie Twilio — WhatsApp et Facebook Messenger.

Un seul adaptateur pour les deux plateformes : Twilio les distingue par le
préfixe d'adresse (`whatsapp:` / `messenger:`), l'API `messages.create` est
identique des deux côtés.

Gère la fenêtre de service de 24h WhatsApp (erreur Twilio 63016 sinon) — cf.
docs/architecture/2026-06-28-twilio-multicanal-v2.md §3.4 : passé ce délai
depuis le dernier message entrant de l'utilisateur, seul un template
pré-approuvé (`ContentSid`) peut être envoyé, pas de texte libre.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from twilio.rest import Client

from jarvis.interfaces.channels.base import ChannelAdapter, IncomingMessage, MessageTarget, Platform
from jarvis.kernel.session_key_store import SessionKeyStore
from jarvis.kernel.settings import settings

_SESSION_WINDOW = timedelta(hours=24)


def _identity_key(address: str) -> str:
    """Normalise une adresse Twilio ('whatsapp:+1514...', 'messenger:<psid>')
    en clé d'identité canonique partagée avec la voix (cf. plan v2 §4.1)."""
    bare = address.split(":", 1)[1] if ":" in address else address
    return f"twilio:{bare}" if address.startswith("whatsapp:") else f"twilio:messenger:{bare}"


class TwilioMessagingChannel(ChannelAdapter):
    """Adaptateur WhatsApp + Messenger via l'API Twilio Messaging."""

    platform = Platform.WHATSAPP  # type: ignore[assignment]

    @property
    def platforms(self) -> tuple[Platform, ...]:
        return (Platform.WHATSAPP, Platform.MESSENGER)

    def __init__(self, session_key_store: SessionKeyStore) -> None:
        self._client = Client(
            settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()
        )
        self._store = session_key_store

    async def start(self) -> None:
        # Canal push (webhook) — rien à démarrer côté client.
        pass

    async def stop(self) -> None:
        pass

    def parse_webhook(self, form: dict) -> IncomingMessage:
        """Convertit un payload de webhook Twilio (form-urlencoded) en IncomingMessage."""
        sender = form.get("From", "")
        platform = Platform.WHATSAPP if sender.startswith("whatsapp:") else Platform.MESSENGER
        self._store.mark_inbound(_identity_key(sender), datetime.now(UTC))
        return IncomingMessage(
            platform=platform,
            user_id=sender,
            text=form.get("Body", ""),
            raw=form,
            identity_key=_identity_key(sender),
        )

    async def send(self, reply: str, target: MessageTarget) -> None:
        is_whatsapp = target.user_id.startswith("whatsapp:")
        from_addr = (
            settings.twilio_whatsapp_number
            if is_whatsapp
            else f"messenger:{settings.twilio_messenger_page_id}"
        )

        if is_whatsapp:
            last_inbound = self._store.last_inbound_at(_identity_key(target.user_id))
            outside_window = last_inbound is None or (datetime.now(UTC) - last_inbound) > _SESSION_WINDOW
            if outside_window:
                await self._send_template_or_skip(from_addr, target.user_id, reply)
                return

        await asyncio.to_thread(
            self._client.messages.create, from_=from_addr, to=target.user_id, body=reply
        )

    async def _send_template_or_skip(self, from_addr: str, to: str, reply: str) -> None:
        """Hors fenêtre 24h WhatsApp : seul un template approuvé passe (erreur 63016 sinon)."""
        if not settings.twilio_whatsapp_template_content_sid:
            logger.warning(
                "Message hors fenêtre de service 24h WhatsApp et aucun "
                "TWILIO_WHATSAPP_TEMPLATE_CONTENT_SID configuré — message abandonné.",
                to=to,
            )
            return
        await asyncio.to_thread(
            self._client.messages.create,
            from_=from_addr,
            to=to,
            content_sid=settings.twilio_whatsapp_template_content_sid,
            content_variables=f'{{"1": "{reply[:1000]}"}}',
        )


__all__ = ["TwilioMessagingChannel"]
