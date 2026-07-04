# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Appels téléphoniques sortants — Twilio Elastic SIP Trunking via LiveKit SIP.

Jarvis ne construit AUCUN TwiML à la main pour la voix : il crée un
participant SIP via l'API LiveKit (`CreateSIPParticipantRequest`), qui route
l'appel à travers le trunk sortant Twilio déjà provisionné côté LiveKit.
Le numéro est validé par une regex E.164 stricte avant tout usage — cf.
audit docs/architecture/2026-06-28-twilio-multicanal-debat.md §3.2 (le plan
initial construisait le TwiML sortant par f-string, vulnérable à l'injection).

Tier confirm systématique : un appel téléphonique est une action externe de
niveau 5 (CDC §10.1), au même titre que LaunchAppTool pour powershell/cmd.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from aiolimiter import AsyncLimiter
from livekit import api as lk_api
from loguru import logger

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.kernel.settings import settings

_APPROVAL_TTL = timedelta(minutes=5)
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")

# CPS Twilio par défaut = 1 appel sortant/seconde (cf. audit §1) — respecté
# ici plutôt qu'ignoré ; Twilio mettrait sinon en file d'attente silencieusement.
_outbound_limiter = AsyncLimiter(1, 1)


class _PendingCall:
    __slots__ = ("numero", "intention", "expires_at")

    def __init__(self, numero: str, intention: str) -> None:
        self.numero = numero
        self.intention = intention
        self.expires_at = datetime.now(UTC) + _APPROVAL_TTL


class PhoneCallTool(Tool):
    """Passe un appel téléphonique sortant via Twilio (transport SIP → LiveKit)."""

    name = "phone_call"
    description = (
        "Passe un appel téléphonique sortant vers un numéro E.164 (ex: +15145551234). "
        "Nécessite une confirmation explicite avant le lancement de l'appel — "
        "action='confirm' pour valider un appel en attente."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "numero": {
                "type": "string",
                "description": "Numéro de téléphone au format E.164, ex: +15145551234.",
            },
            "intention": {
                "type": "string",
                "description": "Ce que Jarvis doit annoncer/accomplir pendant l'appel.",
            },
            "action": {
                "type": "string",
                "enum": ["run", "confirm"],
                "description": "'run' met l'appel en attente d'approbation. 'confirm' le lance.",
            },
        },
        "required": ["numero", "intention"],
    }

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCall] = {}

    async def execute(
        self, numero: str, intention: str = "", action: str = "run", **_: object
    ) -> ToolResult:
        if not _E164.match(numero):
            return ToolResult(
                content=f"Numéro invalide (format E.164 attendu, ex +15145551234) : {numero}",
                is_error=True,
            )

        if action == "confirm":
            return await self._confirm_pending(numero)

        self._pending[numero] = _PendingCall(numero=numero, intention=intention)
        return ToolResult(
            content=(
                f"Appel vers {numero} en attente de confirmation "
                f"({intention or 'sans intention précisée'}).\n"
                f"Dis 'confirme l'appel vers {numero}' pour le lancer (valide 5 minutes)."
            )
        )

    async def _confirm_pending(self, numero: str) -> ToolResult:
        now = datetime.now(UTC)
        expired = [k for k, p in self._pending.items() if p.expires_at <= now]
        for k in expired:
            del self._pending[k]

        pending = self._pending.pop(numero, None)
        if pending is None:
            return ToolResult(
                content=(
                    f"Aucun appel vers {numero} en attente (ou délai de 5 minutes expiré). "
                    "Relance la demande."
                ),
                is_error=True,
            )

        if not settings.livekit_sip_outbound_trunk_id:
            return ToolResult(
                content=(
                    "LIVEKIT_SIP_OUTBOUND_TRUNK_ID non configuré — le trunk SIP sortant "
                    "doit être provisionné côté LiveKit avant de pouvoir appeler."
                ),
                is_error=True,
            )

        room_name = f"jarvis-call-{pending.numero.lstrip('+')}"
        try:
            async with _outbound_limiter:
                async with lk_api.LiveKitAPI() as lk:
                    await lk.sip.create_sip_participant(
                        lk_api.CreateSIPParticipantRequest(
                            sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                            sip_call_to=pending.numero,
                            room_name=room_name,
                            participant_identity=f"sip-{pending.numero.lstrip('+')}",
                            participant_metadata=pending.intention,
                            wait_until_answered=False,
                        )
                    )
        except Exception as e:
            logger.error(
                "Échec de création du participant SIP sortant", numero=numero, error=str(e)
            )
            return ToolResult(content=f"Échec de l'appel vers {numero} : {e}", is_error=True)

        logger.info("Appel sortant initié", numero=numero, room=room_name)
        return ToolResult(content=f"Appel initié vers {numero}.")


__all__ = ["PhoneCallTool"]
