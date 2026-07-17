from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from jarvis.providers.openclaw.client import OpenClawClient


async def _fake_gateway(websocket) -> None:
    connect_frame = json.loads(await websocket.recv())
    assert connect_frame["type"] == "connect"
    assert connect_frame["params"]["auth"]["token"] == "secret-token"
    await websocket.send(json.dumps({"type": "connect-ok"}))

    async for raw in websocket:
        frame = json.loads(raw)
        if frame["type"] == "req" and frame["method"] == "health":
            await websocket.send(
                json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": {"status": "ok"}})
            )


@pytest.mark.asyncio
async def test_connect_puis_health_correles() -> None:
    async with websockets.serve(_fake_gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OpenClawClient(ws_url=f"ws://127.0.0.1:{port}", token="secret-token")
        await client.connect()
        try:
            result = await client.health()
            assert result == {"status": "ok"}
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_side_effect_ajoute_idempotency_key() -> None:
    received: dict = {}

    async def _capture(websocket) -> None:
        await websocket.recv()  # connect
        await websocket.send(json.dumps({"type": "connect-ok"}))
        raw = await websocket.recv()
        frame = json.loads(raw)
        received.update(frame)
        await websocket.send(
            json.dumps({"type": "res", "id": frame["id"], "ok": True, "payload": {}})
        )

    async with websockets.serve(_capture, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OpenClawClient(ws_url=f"ws://127.0.0.1:{port}", token="t")
        await client.connect()
        try:
            await client.request("voicecall.initiate", {"to": "+1555"}, side_effect=True)
        finally:
            await client.close()

    assert "idempotencyKey" in received["params"]


@pytest.mark.asyncio
async def test_rpc_error_propagates_exception() -> None:
    async def _error_gateway(websocket) -> None:
        await websocket.recv()  # connect
        await websocket.send(json.dumps({"type": "connect-ok"}))
        raw = await websocket.recv()
        frame = json.loads(raw)
        await websocket.send(
            json.dumps({
                "type": "res",
                "id": frame["id"],
                "ok": False,
                "error": {"message": "boom"}
            })
        )

    async with websockets.serve(_error_gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OpenClawClient(ws_url=f"ws://127.0.0.1:{port}", token="t")
        await client.connect()
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await client.request("test.method")
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_timeout_cleans_up_pending_entry() -> None:
    async def _slow_gateway(websocket) -> None:
        await websocket.recv()  # connect
        await websocket.send(json.dumps({"type": "connect-ok"}))
        await websocket.recv()  # receive request but never respond

    async with websockets.serve(_slow_gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OpenClawClient(ws_url=f"ws://127.0.0.1:{port}", token="t")
        await client.connect()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await client.request("test.method", timeout=0.1)
            # Verify the pending entry was cleaned up
            assert len(client._pending) == 0
        finally:
            await client.close()
