# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de _resolve_sip_caller_context — détection d'un appel Twilio entrant.

Couvre : room non-SIP ignorée, résolution du numéro appelant via l'attribut
`sip.phoneNumber`, continuité de session avec le canal messagerie (clé
`twilio:{numero}`), timeout sans participant, et résilience si le
SessionKeyStore est inaccessible. Le SessionKeyStore utilisé par la fonction
est redirigé vers un fichier temporaire (`_SIP_SESSION_DB` est monkeypatché) —
aucun test ne touche `memory/messaging_sessions.db` du projet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.interfaces.voice import agent
from jarvis.kernel.session_key_store import SessionKeyStore

_NUMERO = "+15145551234"


class _FakeParticipant:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


class _FakeRoom:
    def __init__(self, name: str, participants: dict | None = None) -> None:
        self.name = name
        self.remote_participants = participants or {}


class _FakeCtx:
    def __init__(self, room: _FakeRoom) -> None:
        self.room = room


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """La boucle d'attente du participant SIP fait 10 x asyncio.sleep(0.2) —
    on la rend instantanée pour ne pas payer 2s réelles par test."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(agent.asyncio, "sleep", _instant_sleep)


@pytest.mark.asyncio
async def test_room_non_sip_retourne_vide() -> None:
    ctx = _FakeCtx(_FakeRoom(name="browser-session-42"))
    result = await agent._resolve_sip_caller_context(ctx)
    assert result == ""


@pytest.mark.asyncio
async def test_room_sip_avec_numero_nouveau_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "_SIP_SESSION_DB", tmp_path / "sessions.db")
    room = _FakeRoom(
        name="jarvis-sip-abc123",
        participants={"p1": _FakeParticipant({"sip.phoneNumber": _NUMERO})},
    )
    result = await agent._resolve_sip_caller_context(_FakeCtx(room))
    assert _NUMERO in result
    assert "nouveau contact" in result


@pytest.mark.asyncio
async def test_room_sip_avec_session_existante(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(agent, "_SIP_SESSION_DB", db_path)
    SessionKeyStore(db_path).persist(f"twilio:{_NUMERO}", "sess-whatsapp-1")

    room = _FakeRoom(
        name="jarvis-sip-abc123",
        participants={"p1": _FakeParticipant({"sip.phoneNumber": _NUMERO})},
    )
    result = await agent._resolve_sip_caller_context(_FakeCtx(room))
    assert _NUMERO in result
    assert "existante" in result


@pytest.mark.asyncio
async def test_room_sip_sans_participant_apres_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "_SIP_SESSION_DB", tmp_path / "sessions.db")
    room = _FakeRoom(name="jarvis-sip-abc123", participants={})
    result = await agent._resolve_sip_caller_context(_FakeCtx(room))
    assert result == ""


@pytest.mark.asyncio
async def test_session_key_store_inaccessible_ne_leve_pas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_path: Path) -> SessionKeyStore:
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(agent, "SessionKeyStore", _boom)
    room = _FakeRoom(
        name="jarvis-sip-abc123",
        participants={"p1": _FakeParticipant({"sip.phoneNumber": _NUMERO})},
    )
    result = await agent._resolve_sip_caller_context(_FakeCtx(room))
    assert "nouveau contact" in result
