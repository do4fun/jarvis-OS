# tests/test_openclaw_integration_seam.py
"""Seam test harnais ACP <-> endpoint `/api/openclaw/message`.

Pin le contrat JSON réel entre `acp_harness.py` (process séparé, hors du
package `jarvis`) et l'endpoint FastAPI `/api/openclaw/message` : ni
`test_acp_harness.py` (serveur HTTP factice fait main) ni
`test_openclaw_api.py` (appelle l'endpoint directement, sans harnais) ne
couvrent la connexion bout-en-bout des deux côtés à la fois.

Option (a) retenue : un vrai serveur FastAPI (uvicorn, thread, port réel)
exposant le vrai routeur `openclaw`, avec `container.gateway`/`voice_gateway`
mockés (pas de LLM réel), et le vrai sous-processus `acp_harness.py` qui lui
parle en HTTP — un round-trip `session/prompt` complet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import uvicorn
from fastapi import FastAPI

from jarvis.interfaces.api.openclaw import router
from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap

HARNESS = (
    Path(__file__).parent.parent / "src" / "jarvis" / "interfaces" / "openclaw" / "acp_harness.py"
)


def _make_app(tmp_path: Path) -> FastAPI:
    session = MagicMock()
    session.id = "sess-seam"
    gw = MagicMock()
    gw.handle = AsyncMock(return_value=(session, "chat", "Réponse depuis Jarvis"))

    app = FastAPI()
    app.state.container = MagicMock(gateway=gw, voice_gateway=gw)
    app.state.openclaw_session_map = OpenClawSessionMap(tmp_path / "s.json")
    app.state.openclaw_client = MagicMock(health=AsyncMock(return_value={"status": "ok"}))
    app.include_router(router)
    return app


class _ServerThread:
    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> int:
        self._thread.start()
        # Attend que le serveur ait bindé son port réel.
        for _ in range(200):
            if getattr(self.server, "started", False):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("uvicorn n'a pas démarré à temps")
        return self.server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)


def _spawn_harness(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["JARVIS_API_URL"] = f"http://127.0.0.1:{port}"
    env["JARVIS_API_TOKEN"] = "t"  # auth désactivée par défaut (api_auth_enabled=False)
    return subprocess.Popen(
        [sys.executable, str(HARNESS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _rpc(
    proc: subprocess.Popen, method: str, params: dict, req_id: int, notifications: list[dict] | None = None
) -> dict:
    frame = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(frame.encode())
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise AssertionError(f"le harnais n'a rien répondu (process terminé) stderr={stderr!r}")
        msg = json.loads(line)
        if msg.get("id") == req_id:
            return msg
        if "id" not in msg and notifications is not None:
            notifications.append(msg)


def test_session_prompt_round_trip_via_vrai_endpoint(tmp_path) -> None:
    app = _make_app(tmp_path)
    server = _ServerThread(app)
    port = server.start()
    try:
        # Sanity check direct : le vrai endpoint répond bien au contrat attendu.
        direct = httpx.post(
            f"http://127.0.0.1:{port}/api/openclaw/message",
            json={"text": "ping", "external_key": "openclaw:direct"},
            timeout=5.0,
        )
        assert direct.status_code == 200
        assert set(direct.json().keys()) == {"reply", "session_id"}

        proc = _spawn_harness(port)
        try:
            _rpc(proc, "initialize", {}, 1)
            resp = _rpc(proc, "session/new", {}, 2)
            session_id = resp["result"]["sessionId"]

            notifications: list[dict] = []
            resp = _rpc(
                proc,
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "Bonjour"}]},
                3,
                notifications=notifications,
            )
            assert resp["result"]["stopReason"] == "end_turn"

            # La notification session/update doit porter la vraie réponse du
            # gateway mocké, prouvant le round-trip complet harnais -> endpoint
            # réel -> container.gateway -> retour au harnais.
            assert len(notifications) == 1
            update = notifications[0]["params"]["update"]
            assert update["text"] == "Réponse depuis Jarvis"
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    finally:
        server.stop()
