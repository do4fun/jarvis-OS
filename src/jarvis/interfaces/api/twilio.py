# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Webhook Twilio — messagerie WhatsApp/Messenger.

La voix n'a PAS de route ici : elle passe par Twilio Elastic SIP Trunking
directement vers LiveKit (cf. docs/architecture/2026-06-28-twilio-multicanal-v2.md
§2.2) — aucun TwiML ni WebSocket Media Streams à servir côté Jarvis.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from loguru import logger
from twilio.request_validator import RequestValidator

from jarvis.kernel.settings import settings

router = APIRouter()


def _reconstruct_public_url(request: Request) -> str:
    """Reconstruit l'URL telle que Twilio l'a signée.

    Cause n°1 documentée des échecs de validation en conditions réelles : un
    reverse proxy termine le TLS et l'app voit http:// alors que Twilio a
    signé https:// (cf. audit §3.1). On lit X-Forwarded-Proto/Host plutôt que
    de concaténer `settings.public_base_url` statiquement.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    path = request.url.path.rstrip("/")
    return f"{proto}://{host}{path}"


async def _validate_signature(request: Request, form: dict) -> bool:
    if not settings.twilio_validate_signature:
        return True
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    signature = request.headers.get("X-Twilio-Signature", "")
    url = _reconstruct_public_url(request)
    return validator.validate(url, form, signature)


@router.post("/api/twilio/messaging/webhook", include_in_schema=False)
async def messaging_webhook(request: Request) -> Response:
    """Webhook messagerie Twilio (WhatsApp/Messenger), POST form-urlencoded."""
    form = dict(await request.form())

    if not await _validate_signature(request, form):
        logger.warning(
            "Signature Twilio invalide sur le webhook messaging — requête rejetée.",
            from_=form.get("From", "?"),
        )
        return Response(status_code=403)

    channel = getattr(request.app.state, "twilio_messaging", None)
    gateway = getattr(request.app.state, "messaging_gateway", None)
    if channel is None or gateway is None:
        logger.error("Webhook Twilio reçu mais TwilioMessagingChannel/MessagingGateway absents.")
        return Response(status_code=503)

    msg = channel.parse_webhook(form)

    async def _dispatch_safe() -> None:
        # Le 200 OK est déjà renvoyé à Twilio avant que cette tâche ne s'exécute
        # (Twilio timeout à 15s) — toute exception ici doit être catchée et
        # logguée, sinon le message est silencieusement perdu sans que Twilio
        # ne retente (cf. audit §2.3).
        try:
            await gateway.dispatch(msg)
        except Exception:
            logger.exception(
                "Échec du dispatch d'un message Twilio — réponse non envoyée à l'utilisateur.",
                from_=form.get("From", "?"),
            )

    asyncio.create_task(_dispatch_safe(), name="twilio-messaging-dispatch")
    return Response(content="<Response/>", media_type="application/xml")


__all__ = ["router"]
