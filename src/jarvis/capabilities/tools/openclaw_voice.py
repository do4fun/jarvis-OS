# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""VoiceCallTool — déclenche un appel téléphonique sortant via le Gateway OpenClaw.

Dépend uniquement de `OpenClawClientProtocol` (kernel/contracts.py), jamais du
provider concret `providers.openclaw.client.OpenClawClient` (RÈGLE 2 —
layering capabilities → kernel, pas capabilities → providers).
"""

from __future__ import annotations

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.kernel.contracts import OpenClawClientProtocol


class VoiceCallTool(Tool):
    name = "voice_call"
    description = (
        "Passe un appel téléphonique sortant via le Gateway OpenClaw (Twilio). "
        "Utilise-le pour rappeler l'utilisateur ou transmettre un message vocal."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Numéro E.164, ex. +15145551234"},
            "message": {"type": "string", "description": "Message à énoncer à la prise d'appel"},
            "mode": {"type": "string", "enum": ["notify", "conversation"], "default": "notify"},
        },
        "required": ["to"],
    }

    def __init__(self, client: OpenClawClientProtocol) -> None:
        self._client = client

    async def execute(self, **kwargs: object) -> ToolResult:
        try:
            payload = {
                "to": kwargs["to"],
                "message": kwargs.get("message", ""),
                "mode": kwargs.get("mode", "notify"),
            }
            result = await self._client.request("voicecall.initiate", payload, side_effect=True)
            return ToolResult(content=f"Appel déclenché ({result.get('callId', '?')}).")
        except Exception as exc:  # noqa: BLE001 — remonté tel quel au LLM comme erreur outil
            return ToolResult(content=f"Échec de l'appel : {exc}", is_error=True)
