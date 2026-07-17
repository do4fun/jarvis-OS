from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from jarvis.interfaces.openclaw.setup import setup_openclaw


@pytest.mark.asyncio
async def test_desactive_par_defaut(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_ENABLED", raising=False)
    app = FastAPI()
    result = await setup_openclaw(app, MagicMock())
    assert result is None
    assert not hasattr(app.state, "openclaw_client")


@pytest.mark.asyncio
async def test_active_attache_client_et_router(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_WS_URL", "ws://127.0.0.1:1")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "t")

    fake_client = MagicMock(connect=AsyncMock())
    monkeypatch.setattr(
        "jarvis.interfaces.openclaw.setup.OpenClawClient", MagicMock(return_value=fake_client)
    )

    app = FastAPI()
    container = MagicMock()
    result = await setup_openclaw(app, container)

    assert result is fake_client
    assert app.state.openclaw_client is fake_client
    assert app.state.openclaw_session_map is not None
    fake_client.connect.assert_awaited_once()
