# tests/test_acp_harness.py
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARNESS = (
    Path(__file__).parent.parent / "src" / "jarvis" / "interfaces" / "openclaw" / "acp_harness.py"
)


class _FakeJarvisHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — imposé par BaseHTTPRequestHandler
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        assert body["external_key"].startswith("openclaw:")
        reply = json.dumps({"reply": f"echo: {body['text']}", "session_id": "sess-x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: object) -> None:
        pass  # silence les logs du serveur factice


class _FailingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.rfile.read(length)
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _start_fake_server(
    handler: type[BaseHTTPRequestHandler] = _FakeJarvisHandler,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _spawn_harness(port: int) -> subprocess.Popen:
    # Windows a besoin de SystemRoot/PATH minimal pour résoudre le runtime
    # Python et ses DLLs — un env vide (comme suggéré par le brief Unix)
    # fait planter le sous-processus avant même d'atteindre le harnais.
    env = dict(os.environ)
    env["JARVIS_API_URL"] = f"http://127.0.0.1:{port}"
    env["JARVIS_API_TOKEN"] = "t"
    return subprocess.Popen(
        [sys.executable, str(HARNESS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _rpc(proc: subprocess.Popen, method: str, params: dict, req_id: int) -> dict:
    """Envoie une requête JSON-RPC et lit la réponse correspondante.

    Le harnais peut émettre des notifications intermédiaires sans "id"
    (ex. `session/update` avant la réponse finale de `session/prompt`) —
    on les ignore et on continue à lire jusqu'à trouver la réponse dont
    l'"id" correspond à la requête envoyée.
    """
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


def test_initialize_new_prompt() -> None:
    server = _start_fake_server()
    port = server.server_address[1]
    proc = _spawn_harness(port)
    try:
        resp = _rpc(proc, "initialize", {}, 1)
        assert resp["id"] == 1

        resp = _rpc(proc, "session/new", {}, 2)
        session_id = resp["result"]["sessionId"]

        resp = _rpc(
            proc,
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "Bonjour"}]},
            3,
        )
        assert resp["result"]["stopReason"] == "end_turn"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        server.shutdown()


def test_session_cancel_no_ops_cleanly() -> None:
    server = _start_fake_server()
    port = server.server_address[1]
    proc = _spawn_harness(port)
    try:
        _rpc(proc, "initialize", {}, 1)
        resp = _rpc(proc, "session/new", {}, 2)
        session_id = resp["result"]["sessionId"]

        resp = _rpc(proc, "session/cancel", {"sessionId": session_id}, 3)
        assert resp["result"]["ok"] is True

        # Annuler une session déjà inconnue/annulée ne doit pas planter.
        resp = _rpc(proc, "session/cancel", {"sessionId": session_id}, 4)
        assert resp["result"]["ok"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        server.shutdown()


def test_http_failure_yields_clean_jsonrpc_error() -> None:
    server = _start_fake_server(_FailingHandler)
    port = server.server_address[1]
    proc = _spawn_harness(port)
    try:
        _rpc(proc, "initialize", {}, 1)
        resp = _rpc(proc, "session/new", {}, 2)
        session_id = resp["result"]["sessionId"]

        resp = _rpc(
            proc,
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "Bonjour"}]},
            3,
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32000

        # Le process doit rester vivant et utilisable après l'erreur HTTP.
        resp = _rpc(proc, "session/cancel", {"sessionId": session_id}, 4)
        assert resp["result"]["ok"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        server.shutdown()
