from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from jarvis.engine.auth import verify_api_token

router = APIRouter(prefix="/api/openclaw", tags=["openclaw"])

_VOICE_CHANNELS = {"voice", "voice-call"}


class OpenClawMessageIn(BaseModel):
    text: str
    external_key: str
    meta: dict[str, Any] | None = None


class OpenClawMessageOut(BaseModel):
    reply: str
    session_id: str


@router.get("/health", dependencies=[Depends(verify_api_token)])
async def openclaw_health(request: Request) -> dict:
    client = request.app.state.openclaw_client
    try:
        status = await client.health()
    except Exception:  # noqa: BLE001 — Gateway injoignable, on reporte un statut dégradé
        return {"status": "unreachable", "connected": False}
    return {"status": status.get("status", "unknown"), "connected": True}


@router.post("/message", dependencies=[Depends(verify_api_token)], response_model=OpenClawMessageOut)
async def openclaw_message(body: OpenClawMessageIn, request: Request) -> OpenClawMessageOut:
    container = request.app.state.container
    session_map = request.app.state.openclaw_session_map

    channel = (body.meta or {}).get("channel")
    gw = container.voice_gateway if channel in _VOICE_CHANNELS else container.gateway

    session_id = session_map.get(body.external_key)
    session, _route, reply = await gw.handle(body.text, session_id=session_id, stream=False)
    session_map.set(body.external_key, str(session.id))

    return OpenClawMessageOut(reply=reply, session_id=str(session.id))
