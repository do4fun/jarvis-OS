# src/jarvis/interfaces/openclaw/acp_harness.py
"""Harnais ACP minimal (stdlib + httpx uniquement) — pont OpenClaw <-> Jarvis.

Spawné par le plugin acpx du Gateway OpenClaw (stdio JSON-RPC). N'importe
PAS le package `jarvis` complet : il tourne dans le processus/l'environnement
du Gateway, pas dans celui de l'API FastAPI. Un sous-ensemble volontairement
restreint du protocole ACP (agentclientprotocol.com) suffit ici : initialize,
session/new, session/prompt, session/cancel.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import httpx

JARVIS_API_URL = os.environ.get("JARVIS_API_URL", "http://127.0.0.1:8000")
JARVIS_API_TOKEN = os.environ.get("JARVIS_API_TOKEN", "")
TIMEOUT = float(os.environ.get("OPENCLAW_ACP_TIMEOUT", "300"))

_sessions: dict[str, dict] = {}


def _reply(req_id: int, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(req_id: int, message: str) -> None:
    payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message}}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _handle_initialize(req_id: int, _params: dict) -> None:
    _reply(req_id, {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}})


def _handle_session_new(req_id: int, params: dict) -> None:
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {"meta": params.get("meta", {})}
    _reply(req_id, {"sessionId": session_id})


def _handle_session_prompt(req_id: int, params: dict) -> None:
    session_id = params["sessionId"]
    blocks = params.get("prompt", [])
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    meta = _sessions.get(session_id, {}).get("meta", {})

    try:
        resp = httpx.post(
            f"{JARVIS_API_URL}/api/openclaw/message",
            json={"text": text, "external_key": f"openclaw:{session_id}", "meta": meta},
            headers={"Authorization": f"Bearer {JARVIS_API_TOKEN}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        reply = resp.json()["reply"]
    except httpx.HTTPError as exc:
        _error(req_id, f"Jarvis injoignable : {exc}")
        return

    update = {"type": "agent_message_chunk", "text": reply}
    notification = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }
    sys.stdout.write(json.dumps(notification) + "\n")
    sys.stdout.flush()
    _reply(req_id, {"stopReason": "end_turn"})


def _handle_session_cancel(req_id: int, params: dict) -> None:
    _sessions.pop(params.get("sessionId", ""), None)
    _reply(req_id, {"ok": True})


_HANDLERS = {
    "initialize": _handle_initialize,
    "session/new": _handle_session_new,
    "session/prompt": _handle_session_prompt,
    "session/cancel": _handle_session_cancel,
}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        frame = json.loads(line)
        handler = _HANDLERS.get(frame["method"])
        if handler is None:
            req_id = frame.get("id")
            if req_id is None:
                # Notification sans id : aucune réponse possible, on ignore.
                sys.stderr.write(f"Méthode inconnue (notification) : {frame.get('method')}\n")
                sys.stderr.flush()
                continue
            _error(req_id, f"Méthode inconnue : {frame['method']}")
            continue
        handler(frame["id"], frame.get("params", {}))


if __name__ == "__main__":
    main()
