from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable

import websockets
from loguru import logger
from websockets.asyncio.client import ClientConnection


class OpenClawClient:
    """Client WS du Gateway OpenClaw — corrélation req/res, events.

    Se connecte paresseusement au premier `request()` si `connect()` n'a pas
    déjà été appelé explicitement (cf. `request()`).

    Nommage volontairement distinct de `engine.gateway.Gateway` (cf. convention
    anti-collision du chantier OpenClaw).
    """

    def __init__(self, ws_url: str, token: str) -> None:
        self._ws_url = ws_url
        self._token = token
        self._conn: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._conn = await websockets.connect(self._ws_url)
        await self._conn.send(json.dumps({"type": "connect", "params": {"auth": {"token": self._token}}}))
        await self._conn.recv()  # connect-ok
        self._reader_task = asyncio.create_task(self._read_loop(), name="openclaw-client-reader")

    async def _read_loop(self) -> None:
        assert self._conn is not None
        try:
            async for raw in self._conn:
                frame = json.loads(raw)
                if frame["type"] == "res":
                    future = self._pending.pop(frame["id"], None)
                    if future is not None and not future.done():
                        if frame.get("ok") is False:
                            error_msg = frame.get("error", {}).get("message", "OpenClaw RPC error")
                            future.set_exception(RuntimeError(error_msg))
                        else:
                            future.set_result(frame.get("payload", {}))
                elif frame["type"] == "event":
                    for cb in self._event_handlers.get(frame["event"], []):
                        cb(frame.get("payload", {}))
        except websockets.ConnectionClosed:
            logger.warning("OpenClawClient — connexion Gateway fermée")

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        side_effect: bool = False,
        timeout: float = 10.0,
    ) -> dict:
        if self._conn is None:
            await self.connect()
        req_id = str(uuid.uuid4())
        payload = dict(params or {})
        if side_effect:
            payload.setdefault("idempotencyKey", str(uuid.uuid4()))
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._conn.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": payload}))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    def on_event(self, event: str, callback: Callable[[dict], None]) -> None:
        self._event_handlers.setdefault(event, []).append(callback)

    async def health(self) -> dict:
        return await self.request("health")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._conn is not None:
            await self._conn.close()
