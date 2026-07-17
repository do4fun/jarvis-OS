from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.capabilities.tools.openclaw_voice import VoiceCallTool


@pytest.mark.asyncio
async def test_execute_appelle_voicecall_initiate_en_side_effect() -> None:
    client = MagicMock(request=AsyncMock(return_value={"callId": "call-1"}))
    tool = VoiceCallTool(client=client)

    result = tool.to_claude_schema()
    assert result["name"] == "voice_call"

    result = await tool.execute(to="+15145551234", message="Rappel : rendez-vous à 15h")

    assert result.is_error is False
    client.request.assert_awaited_once()
    args, kwargs = client.request.await_args
    assert args[0] == "voicecall.initiate"
    assert args[1]["to"] == "+15145551234"
    assert kwargs["side_effect"] is True


@pytest.mark.asyncio
async def test_execute_remonte_erreur_rpc_proprement() -> None:
    client = MagicMock(request=AsyncMock(side_effect=RuntimeError("Gateway down")))
    tool = VoiceCallTool(client=client)

    result = await tool.execute(to="+15145551234")

    assert result.is_error is True
    assert "Gateway down" in result.content
