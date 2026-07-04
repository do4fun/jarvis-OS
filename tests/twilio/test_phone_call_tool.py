# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de PhoneCallTool — appel sortant Twilio via participant SIP LiveKit.

Couvre : validation E.164, flow run/confirm avec expiration, garde-fou trunk
non configuré, appel LiveKit mocké (bons paramètres), propagation d'erreur
sans exception, usage du rate-limiter. Aucun appel réseau réel — LiveKitAPI
est entièrement mocké.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.capabilities.tools import phone_call
from jarvis.capabilities.tools.phone_call import PhoneCallTool

_NUMERO = "+15145551234"
_INTENTION = "Confirmer le rendez-vous de demain"


class _FakeSipClient:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.side_effect: Exception | None = None

    async def create_sip_participant(self, request: object) -> None:
        if self.side_effect is not None:
            raise self.side_effect
        self.calls.append(request)


class _FakeLiveKitAPI:
    def __init__(self, sip_client: _FakeSipClient) -> None:
        self.sip = sip_client

    async def __aenter__(self) -> _FakeLiveKitAPI:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _fast_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remplace le rate-limiter partagé par une variante à haut débit.

    `_outbound_limiter` (module-level, 1 appel/s) est partagé entre TOUTES les
    invocations de confirm() du process pytest — sans ce fixture, plusieurs
    tests de ce fichier se mettraient en attente les uns derrière les autres.
    """
    from aiolimiter import AsyncLimiter

    monkeypatch.setattr(phone_call, "_outbound_limiter", AsyncLimiter(1000, 1))


def _fake_livekit_api_factory(sip_client: _FakeSipClient) -> object:
    def _factory() -> _FakeLiveKitAPI:
        return _FakeLiveKitAPI(sip_client)

    return _factory


@pytest.mark.asyncio
async def test_numero_invalide_rejete() -> None:
    tool = PhoneCallTool()
    result = await tool.execute(numero="0145551234", intention=_INTENTION)
    assert result.is_error
    assert "0145551234" in result.content
    assert tool._pending == {}


@pytest.mark.asyncio
async def test_run_met_en_attente_sans_appeler_livekit(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = PhoneCallTool()

    def _boom() -> None:
        raise AssertionError("LiveKitAPI ne doit pas être appelé pour action='run'")

    monkeypatch.setattr(phone_call.lk_api, "LiveKitAPI", _boom)

    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    assert not result.is_error
    assert _NUMERO in tool._pending


@pytest.mark.asyncio
async def test_confirm_sans_pending_echoue() -> None:
    tool = PhoneCallTool()
    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")
    assert result.is_error
    assert _NUMERO in result.content


@pytest.mark.asyncio
async def test_confirm_apres_expiration_echoue() -> None:
    tool = PhoneCallTool()
    await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    tool._pending[_NUMERO].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")
    assert result.is_error
    assert _NUMERO not in tool._pending


@pytest.mark.asyncio
async def test_confirm_sans_trunk_configure_echoue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_call.settings, "livekit_sip_outbound_trunk_id", "")

    def _boom() -> None:
        raise AssertionError("LiveKitAPI ne doit pas être appelé sans trunk configuré")

    monkeypatch.setattr(phone_call.lk_api, "LiveKitAPI", _boom)

    tool = PhoneCallTool()
    await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")

    assert result.is_error
    assert "LIVEKIT_SIP_OUTBOUND_TRUNK_ID" in result.content


@pytest.mark.asyncio
async def test_confirm_cree_le_bon_participant_sip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_call.settings, "livekit_sip_outbound_trunk_id", "trunk-abc")
    sip_client = _FakeSipClient()
    monkeypatch.setattr(phone_call.lk_api, "LiveKitAPI", _fake_livekit_api_factory(sip_client))

    tool = PhoneCallTool()
    await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")

    assert not result.is_error
    assert len(sip_client.calls) == 1
    request = sip_client.calls[0]
    assert request.sip_trunk_id == "trunk-abc"
    assert request.sip_call_to == _NUMERO
    assert request.room_name == "jarvis-call-15145551234"
    assert request.participant_identity == "sip-15145551234"
    assert request.participant_metadata == _INTENTION
    assert request.wait_until_answered is False
    assert _NUMERO not in tool._pending


@pytest.mark.asyncio
async def test_echec_livekit_renvoie_erreur_sans_lever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_call.settings, "livekit_sip_outbound_trunk_id", "trunk-abc")
    sip_client = _FakeSipClient()
    sip_client.side_effect = RuntimeError("trunk indisponible")
    monkeypatch.setattr(phone_call.lk_api, "LiveKitAPI", _fake_livekit_api_factory(sip_client))

    tool = PhoneCallTool()
    await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")

    assert result.is_error
    assert _NUMERO in result.content


@pytest.mark.asyncio
async def test_rate_limiter_est_utilise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_call.settings, "livekit_sip_outbound_trunk_id", "trunk-abc")
    sip_client = _FakeSipClient()
    monkeypatch.setattr(phone_call.lk_api, "LiveKitAPI", _fake_livekit_api_factory(sip_client))

    acquire_calls: list[bool] = []
    original_acquire = phone_call._outbound_limiter.acquire

    async def _spy_acquire(*args: object, **kwargs: object) -> None:
        acquire_calls.append(True)
        await original_acquire(*args, **kwargs)

    monkeypatch.setattr(phone_call._outbound_limiter, "acquire", _spy_acquire)

    tool = PhoneCallTool()
    await tool.execute(numero=_NUMERO, intention=_INTENTION, action="run")
    result = await tool.execute(numero=_NUMERO, intention=_INTENTION, action="confirm")

    assert not result.is_error
    assert acquire_calls, "le rate limiter n'a pas été utilisé avant l'appel LiveKit"
    assert len(sip_client.calls) == 1
