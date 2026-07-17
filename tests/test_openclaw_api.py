from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.interfaces.api.openclaw import router


def _make_app(gateway: MagicMock, voice_gateway: MagicMock, session_map) -> TestClient:
    app = FastAPI()
    app.state.container = MagicMock(gateway=gateway, voice_gateway=voice_gateway)
    app.state.openclaw_session_map = session_map
    app.state.openclaw_client = MagicMock(health=AsyncMock(return_value={"status": "ok"}))
    app.include_router(router)
    return TestClient(app)


def _gw(reply: str = "Bonjour !") -> MagicMock:
    session = MagicMock()
    session.id = "sess-1"
    gw = MagicMock()
    gw.handle = AsyncMock(return_value=(session, "chat", reply))
    return gw


def test_health(monkeypatch) -> None:
    monkeypatch.setattr("jarvis.interfaces.api.openclaw.verify_api_token", AsyncMock(return_value=None))
    from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

    client = _make_app(_gw(), _gw(), OpenClawSessionMap.__new__(OpenClawSessionMap))
    resp = client.get("/api/openclaw/health")
    assert resp.status_code == 200
    assert resp.json()["connected"] is True


def test_health_gateway_injoignable_retourne_200_deconnecte(monkeypatch) -> None:
    monkeypatch.setattr("jarvis.interfaces.api.openclaw.verify_api_token", AsyncMock(return_value=None))
    from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

    client = _make_app(_gw(), _gw(), OpenClawSessionMap.__new__(OpenClawSessionMap))
    client.app.state.openclaw_client = MagicMock(
        health=AsyncMock(side_effect=RuntimeError("OpenClawClient.connect() n'a pas été appelé"))
    )
    resp = client.get("/api/openclaw/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "unreachable", "connected": False}


def test_message_route_texte_par_defaut(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("jarvis.interfaces.api.openclaw.verify_api_token", AsyncMock(return_value=None))
    from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

    text_gw = _gw("Réponse texte")
    voice_gw = _gw("Réponse voix")
    client = _make_app(text_gw, voice_gw, OpenClawSessionMap(tmp_path / "s.json"))

    resp = client.post("/api/openclaw/message", json={"text": "salut", "external_key": "openclaw:1"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "Réponse texte", "session_id": "sess-1"}
    text_gw.handle.assert_awaited_once()
    voice_gw.handle.assert_not_awaited()


def test_message_route_voix_utilise_voice_gateway(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("jarvis.interfaces.api.openclaw.verify_api_token", AsyncMock(return_value=None))
    from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

    text_gw = _gw("Réponse texte")
    voice_gw = _gw("Réponse voix")
    client = _make_app(text_gw, voice_gw, OpenClawSessionMap(tmp_path / "s.json"))

    resp = client.post(
        "/api/openclaw/message",
        json={"text": "rappelle-moi l'heure", "external_key": "openclaw:2", "meta": {"channel": "voice-call"}},
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Réponse voix"
    voice_gw.handle.assert_awaited_once()
    text_gw.handle.assert_not_awaited()


def test_meme_external_key_reutilise_la_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("jarvis.interfaces.api.openclaw.verify_api_token", AsyncMock(return_value=None))
    from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

    gw = _gw()
    client = _make_app(gw, gw, OpenClawSessionMap(tmp_path / "s.json"))

    client.post("/api/openclaw/message", json={"text": "un", "external_key": "openclaw:3"})
    client.post("/api/openclaw/message", json={"text": "deux", "external_key": "openclaw:3"})

    calls = gw.handle.await_args_list
    assert calls[0].kwargs["session_id"] is None
    assert calls[1].kwargs["session_id"] == "sess-1"
