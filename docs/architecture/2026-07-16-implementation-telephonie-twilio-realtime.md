# Téléphonie Twilio Realtime via OpenClaw — Plan d'implémentation

> **Pour les exécutants agentiques :** SOUS-SKILL REQUIS — utiliser `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans` pour exécuter ce plan tâche par tâche. Les étapes utilisent la syntaxe case à cocher (`- [ ]`) pour le suivi.

**But :** faire répondre les appels téléphoniques Twilio via le Gateway OpenClaw (plugin voice-call, mode **realtime**) tout en gardant **Jarvis comme cerveau** pour tout ce qui est décision/outil/mémoire — sans casser le pipeline LiveKit/SIP existant.

**Architecture :** le modèle realtime d'OpenClaw (Gemini Live) gère la mécanique conversationnelle (accusés, barge-in) ; dès qu'il faut une décision, un outil ou de la mémoire, il appelle le tool intégré `openclaw_agent_consult`, routé par un binding ACP vers un harnais Python (`jarvis-acp`) qui relaie en HTTP vers un nouvel endpoint FastAPI (`/api/openclaw/message`), lequel appelle `engine.Gateway.handle()` — en sélectionnant `container.voice_gateway` (LLM rapide, déjà utilisé par LiveKit) plutôt que `container.gateway` quand `meta.channel` indique un canal vocal. Un client WS générique (`OpenClawClient`) sert à la fois à ce flux entrant et aux appels sortants (tool `voice_call`).

**Tech Stack :** Python 3.11+, FastAPI, `websockets` (nouvelle dépendance directe), httpx (déjà présent), pytest + pytest-asyncio, OpenClaw Gateway (Node.js, exécuté en WSL2 pendant le dev — cf. décision utilisateur, hors périmètre de ce plan de code).

## Contexte et documents liés

Ce plan est le premier livrable **concret et exécutable** du chantier OpenClaw. Il s'appuie sur trois documents d'étude déjà versionnés dans `docs/architecture/` et **volontairement non modifiés** (traces de réflexion à conserver telles quelles) :
- `2026-07-08-openclaw-gateway-integration-v1.md` — plan-cadre en 6 briques (contexte général, conventions de nommage, anti-collision `Gateway`).
- `2026-07-16 - Plan d'intégration Twilio via Gateway OpenClaw.md` — arbitrage des 3 stratégies de latence voix ; ce plan implémente la **Stratégie A (nominale)** + le socle de la **Stratégie B** (streaming/route rapide), recommandée en « B2 en deux temps ».
- `2026-07-09 - Plan Realtime maison - Intégration du Gateway_v2.md` — stack self-hosted (Kyutai/Unmute), gardée en réserve comme implémentation de la **Stratégie C** (secours) si les mesures montrent un p90 > 2,5 s après ce plan — non traitée ici.

## Global Constraints

- **Couches (import-linter, `pyproject.toml`)** : `kernel` ← `providers`/`capabilities` ← `engine` ← `interfaces`. Les tools (`capabilities/`) ne peuvent pas importer `providers/` directement — ils typent sur un `Protocol` de `kernel/contracts.py`, injecté par `bootstrap.build()`.
- **Anti-collision de nom** : `engine/gateway.py::Gateway` existe déjà. Tout ce qui touche OpenClaw porte le préfixe/dossier `openclaw` (`OpenClawClient`, `providers/openclaw/`, `interfaces/openclaw/`, routes `/api/openclaw/*`) — jamais « Gateway » nu dans un nom Jarvis.
- **Dépendance** : ajouter `"websockets>=13.0"` dans `[project.dependencies]` de `pyproject.toml` — aujourd'hui seulement transitif via `uvicorn[standard]`.
- **Convention de tests** : fichiers plats `tests/test_*.py` (pas de sous-dossiers, à l'exception de `tests/unit/kernel/` déjà existant) ; `pytest-asyncio` en `asyncio_mode=auto` (pas besoin de `@pytest.mark.asyncio`).
- **RPC side-effecting** (`voicecall.*`, `chat.send`) exigent un `idempotencyKey` (uuid4) dans `params`.
- **Secrets** : tout credential du plugin voice-call (Twilio `authToken`, clés `realtime.providers.*.apiKey`, `tts.providers.*.apiKey`) passe par un **SecretRef** OpenClaw — jamais en clair dans `config/openclaw.json` versionné.
- **Ne pas construire** : de nouveau `ChannelAdapter` Jarvis, de portage des tools de la branche `feature/twilio-multicanal`, de pont MCP, de mode « transcription seule » (remplacé par le mode realtime décrit ici).

---

### Task 1: `OpenClawClient` — client WebSocket du Gateway (provider L1)

**Files:**
- Create: `src/jarvis/providers/openclaw/__init__.py`
- Create: `src/jarvis/providers/openclaw/client.py`
- Test: `tests/test_openclaw_client.py`

**Interfaces:**
- Produces : `OpenClawClient(ws_url: str, token: str)`, `async connect() -> None`, `async request(method: str, params: dict | None = None, *, side_effect: bool = False, timeout: float = 10.0) -> dict`, `def on_event(event: str, callback: Callable[[dict], None]) -> None`, `async health() -> dict`, `async close() -> None`.

- [ ] **Step 1: Écrire le test qui échoue — handshake + corrélation req/res**

```python
# tests/test_openclaw_client.py
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
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_openclaw_client.py -v`
Expected: FAIL avec `ModuleNotFoundError: No module named 'jarvis.providers.openclaw'`

- [ ] **Step 3: Implémenter `OpenClawClient`**

```python
# src/jarvis/providers/openclaw/__init__.py
```
(fichier vide — marque le package)

```python
# src/jarvis/providers/openclaw/client.py
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable

import websockets
from loguru import logger
from websockets.asyncio.client import ClientConnection


class OpenClawClient:
    """Client WS du Gateway OpenClaw — corrélation req/res, events, reconnexion.

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
            raise RuntimeError("OpenClawClient.connect() n'a pas été appelé")
        req_id = str(uuid.uuid4())
        payload = dict(params or {})
        if side_effect:
            payload.setdefault("idempotencyKey", str(uuid.uuid4()))
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._conn.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": payload}))
        return await asyncio.wait_for(future, timeout=timeout)

    def on_event(self, event: str, callback: Callable[[dict], None]) -> None:
        self._event_handlers.setdefault(event, []).append(callback)

    async def health(self) -> dict:
        return await self.request("health")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._conn is not None:
            await self._conn.close()
```

- [ ] **Step 4: Lancer le test pour vérifier qu'il passe**

Run: `uv run pytest tests/test_openclaw_client.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Ajouter la dépendance et vérifier les imports en couches**

```bash
# pyproject.toml — dans [project] dependencies, ajouter :
#   "websockets>=13.0",
uv sync
uv run lint-imports
```
Expected: `lint-imports` vert (le module ne dépend que de `websockets`/stdlib — pas d'import `jarvis.*` hors kernel).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/jarvis/providers/openclaw tests/test_openclaw_client.py
git commit -m "feat(openclaw): client WS OpenClawClient (provider L1)"
```

---

### Task 2: `OpenClawClientProtocol` — contrat kernel

**Files:**
- Modify: `src/jarvis/kernel/contracts.py` (ajout en fin de fichier, section L1 — Providers)
- Test: `tests/unit/kernel/test_contracts_conformance.py` (existant — ajouter un cas)

**Interfaces:**
- Consumes: rien (Protocol pur).
- Produces: `OpenClawClientProtocol` — méthodes `request`, `health`, `on_event`, utilisé par les tools de la Task 7 sans importer `providers.openclaw` directement.

- [ ] **Step 1: Lire le test de conformité existant pour connaître le pattern attendu**

Run: `uv run pytest tests/unit/kernel/test_contracts_conformance.py -v --collect-only`
(confirme le nom du test à dupliquer pour un nouveau Protocol — pas de code à écrire ici, juste vérifier le format des assertions `isinstance(..., Protocol)` existantes).

- [ ] **Step 2: Ajouter le Protocol dans `contracts.py`**

```python
# src/jarvis/kernel/contracts.py — à la fin du bloc "L1 — Providers" (après TTSEngine)


@runtime_checkable
class OpenClawClientProtocol(Protocol):
    """Client du Gateway OpenClaw (cf. providers/openclaw/client.py).

    Permet aux tools (`capabilities/tools/openclaw_voice.py`) de dépendre
    du contrat kernel plutôt que d'importer `providers.openclaw` (RÈGLE 2).
    """

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        side_effect: bool = False,
        timeout: float = 10.0,
    ) -> dict: ...

    async def health(self) -> dict: ...

    def on_event(self, event: str, callback: Callable[[dict], None]) -> None: ...
```

- [ ] **Step 3: Ajouter le test de conformité**

```python
# tests/unit/kernel/test_contracts_conformance.py — ajouter :
from jarvis.kernel.contracts import OpenClawClientProtocol
from jarvis.providers.openclaw.client import OpenClawClient


def test_openclaw_client_conforme_au_protocol() -> None:
    client = OpenClawClient(ws_url="ws://x", token="t")
    assert isinstance(client, OpenClawClientProtocol)
```

- [ ] **Step 4: Lancer les tests**

Run: `uv run pytest tests/unit/kernel/test_contracts_conformance.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/kernel/contracts.py tests/unit/kernel/test_contracts_conformance.py
git commit -m "feat(openclaw): contrat OpenClawClientProtocol (kernel)"
```

---

### Task 3: Store de mapping de sessions OpenClaw

**Files:**
- Create: `src/jarvis/interfaces/openclaw/__init__.py`
- Create: `src/jarvis/interfaces/openclaw/session_map.py`
- Test: `tests/test_openclaw_session_map.py`

**Interfaces:**
- Produces: `OpenClawSessionMap(path: Path)`, `get(external_key: str) -> str | None`, `set(external_key: str, session_id: str) -> None`.
- Consumes (Task 4) : instanciée avec `memory_dir / "openclaw_sessions.json"` (même dossier que `memory/messaging_sessions.json`, pattern existant dans `interfaces/channels/gateway.py`).

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_openclaw_session_map.py
from __future__ import annotations

import json

from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap


def test_set_puis_get(tmp_path) -> None:
    store = OpenClawSessionMap(tmp_path / "openclaw_sessions.json")
    assert store.get("openclaw:abc") is None
    store.set("openclaw:abc", "sess-1")
    assert store.get("openclaw:abc") == "sess-1"


def test_persiste_sur_disque(tmp_path) -> None:
    path = tmp_path / "openclaw_sessions.json"
    OpenClawSessionMap(path).set("openclaw:abc", "sess-1")
    reloaded = OpenClawSessionMap(path)
    assert reloaded.get("openclaw:abc") == "sess-1"
    assert json.loads(path.read_text()) == {"openclaw:abc": "sess-1"}
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_openclaw_session_map.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implémenter**

```python
# src/jarvis/interfaces/openclaw/__init__.py
```
(fichier vide)

```python
# src/jarvis/interfaces/openclaw/session_map.py
from __future__ import annotations

import json
from pathlib import Path


class OpenClawSessionMap:
    """Mapping `external_key -> jarvis session_id`, persisté en JSON.

    Copie du pattern `_session_map` de `interfaces/channels/gateway.py`,
    dans un fichier séparé (`openclaw_sessions.json`) pour ne pas mélanger
    avec `messaging_sessions.json`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, external_key: str) -> str | None:
        return self._data.get(external_key)

    def set(self, external_key: str, session_id: str) -> None:
        self._data[external_key] = session_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data), encoding="utf-8")
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `uv run pytest tests/test_openclaw_session_map.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/interfaces/openclaw/__init__.py src/jarvis/interfaces/openclaw/session_map.py tests/test_openclaw_session_map.py
git commit -m "feat(openclaw): store de mapping de sessions OpenClaw"
```

---

### Task 4: Endpoints `/api/openclaw/health` et `/api/openclaw/message` (avec route rapide voix)

**Files:**
- Create: `src/jarvis/interfaces/api/openclaw.py`
- Test: `tests/test_openclaw_api.py`

**Interfaces:**
- Consumes : `container.gateway.handle(message, session_id=None, stream=False) -> tuple[Session, RouteEnum, str]` (signature réelle, `src/jarvis/engine/gateway.py:59`) ; `container.voice_gateway` (même signature — instance dédiée au LLM voix, déjà construite en bootstrap) ; `OpenClawSessionMap` (Task 3) ; `verify_api_token` (`src/jarvis/engine/auth.py:43`).
- Produces : `router: APIRouter` monté sous `/api/openclaw` (Task 6 le monte dans l'app).

**Décision route rapide (Stratégie B, socle)** : `container.voice_gateway` existe déjà (construit dans `bootstrap.py`, utilisé aujourd'hui par le pipeline LiveKit avec un LLM plus rapide/économique). Pour la voix OpenClaw, on réutilise **cette même instance** plutôt que d'en créer une troisième — `meta.channel in {"voice", "voice-call"}` route vers `voice_gateway`, tout le reste vers `gateway`.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_openclaw_api.py
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
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_openclaw_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jarvis.interfaces.api.openclaw'`

- [ ] **Step 3: Implémenter le router**

```python
# src/jarvis/interfaces/api/openclaw.py
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
    status = await client.health()
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
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `uv run pytest tests/test_openclaw_api.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/interfaces/api/openclaw.py tests/test_openclaw_api.py
git commit -m "feat(openclaw): endpoints /api/openclaw/health et /message (route rapide voix)"
```

---

### Task 5: Harnais ACP (`jarvis-acp`) — pont stdio JSON-RPC vers l'endpoint

**Files:**
- Create: `src/jarvis/interfaces/openclaw/acp_harness.py`
- Test: `tests/test_acp_harness.py`

**Interfaces:**
- Consumes : `POST {JARVIS_API_URL}/api/openclaw/message` (Task 4) via `httpx`.
- Produces : process stdio JSON-RPC minimal — `initialize`, `session/new`, `session/prompt`, `session/cancel`. **Imports stdlib + httpx uniquement** (exécuté par acpx, pas de `bootstrap.build()`, pas de dépendance au reste de `jarvis`).
- Env : `JARVIS_API_URL` (défaut `http://127.0.0.1:8000`), `JARVIS_API_TOKEN`.

- [ ] **Step 1: Écrire le test qui échoue — un serveur HTTP factice + le harnais en sous-processus**

```python
# tests/test_acp_harness.py
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARNESS = Path(__file__).parent.parent / "src" / "jarvis" / "interfaces" / "openclaw" / "acp_harness.py"


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


def _start_fake_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeJarvisHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _rpc(proc: subprocess.Popen, method: str, params: dict, req_id: int) -> dict:
    frame = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
    proc.stdin.write(frame.encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


def test_initialize_new_prompt() -> None:
    server = _start_fake_server()
    port = server.server_address[1]
    proc = subprocess.Popen(
        [sys.executable, str(HARNESS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={"JARVIS_API_URL": f"http://127.0.0.1:{port}", "JARVIS_API_TOKEN": "t", "PATH": ""},
    )
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
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_acp_harness.py -v`
Expected: FAIL — le fichier `acp_harness.py` n'existe pas encore (le sous-processus se termine immédiatement, pas de ligne stdout).

- [ ] **Step 3: Implémenter le harnais**

```python
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
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message}}) + "\n"
    )
    sys.stdout.flush()


def _handle_initialize(req_id: int, _params: dict) -> None:
    _reply(req_id, {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}})


def _handle_session_new(req_id: int, params: dict) -> None:
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {"meta": params.get("meta", {})}
    _reply(req_id, {"sessionId": session_id})


def _handle_session_prompt(req_id: int, params: dict) -> None:
    session_id = params["sessionId"]
    text = "".join(block.get("text", "") for block in params.get("prompt", []) if block.get("type") == "text")
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

    sys.stdout.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": {"type": "agent_message_chunk", "text": reply}},
            }
        )
        + "\n"
    )
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
            _error(frame["id"], f"Méthode inconnue : {frame['method']}")
            continue
        handler(frame["id"], frame.get("params", {}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `uv run pytest tests/test_acp_harness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/interfaces/openclaw/acp_harness.py tests/test_acp_harness.py
git commit -m "feat(openclaw): harnais ACP jarvis-acp (stdio JSON-RPC)"
```

---

### Task 6: Wiring au boot — `interfaces/openclaw/setup.py` + `app.py`

**Files:**
- Create: `src/jarvis/interfaces/openclaw/setup.py`
- Modify: `src/jarvis/app.py:57` (import), `src/jarvis/app.py:255` (appel dans `lifespan`, juste après `setup_channels`)
- Test: `tests/test_openclaw_setup.py`

**Interfaces:**
- Consumes : `Container` (`src/jarvis/bootstrap.py:118`), pattern `setup_channels(app, container) -> MessagingGateway | None` (`src/jarvis/interfaces/channels/setup.py:37`) à répliquer.
- Produces : `async def setup_openclaw(app: FastAPI, container: Container) -> OpenClawClient | None`.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_openclaw_setup.py
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
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_openclaw_setup.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implémenter**

```python
# src/jarvis/interfaces/openclaw/setup.py
"""Wiring OpenClaw au boot du process API — hors-Container par design,
même statut que `interfaces/channels/setup.py::setup_channels` (CDC §C.1).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import FastAPI
from loguru import logger

from jarvis.interfaces.api.openclaw import router as openclaw_router
from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap
from jarvis.kernel.paths import MEMORY_DIR
from jarvis.providers.openclaw.client import OpenClawClient

if TYPE_CHECKING:
    from jarvis.bootstrap import Container


async def setup_openclaw(app: FastAPI, container: Container) -> OpenClawClient | None:
    if os.getenv("OPENCLAW_ENABLED", "false").lower() != "true":
        return None

    ws_url = os.environ["OPENCLAW_WS_URL"]
    token = os.environ["OPENCLAW_GATEWAY_TOKEN"]

    client = OpenClawClient(ws_url=ws_url, token=token)
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001 — tolérant : Gateway peut démarrer après Jarvis
        logger.warning("OpenClaw Gateway injoignable au boot", error=str(exc))

    app.state.container = container
    app.state.openclaw_client = client
    app.state.openclaw_session_map = OpenClawSessionMap(MEMORY_DIR / "openclaw_sessions.json")
    app.include_router(openclaw_router)
    logger.info("OpenClaw wiring actif", ws_url=ws_url)
    return client
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `uv run pytest tests/test_openclaw_setup.py -v`
Expected: PASS

- [ ] **Step 5: Brancher dans `app.py`**

```python
# src/jarvis/app.py — ajouter à côté de l'import existant ligne 57 :
from jarvis.interfaces.openclaw.setup import setup_openclaw
```

```python
# src/jarvis/app.py — dans lifespan(), juste après la ligne 255 (_messaging_gw = await setup_channels(...)) :
    _openclaw_client = await setup_openclaw(app, container)
```

Vérifier que `MEMORY_DIR` existe bien dans `jarvis.kernel.paths` (utilisé ailleurs dans le repo pour le même usage) :
Run: `uv run python -c "from jarvis.kernel.paths import MEMORY_DIR; print(MEMORY_DIR)"`
Expected: un chemin absolu s'affiche sans erreur.

- [ ] **Step 6: Vérifier que le serveur démarre toujours sans régression (flag off)**

Run: `uv run python -m jarvis.app &` puis `curl -s http://127.0.0.1:8000/docs -o /dev/null -w "%{http_code}\n"` (avec `OPENCLAW_ENABLED` absent ou `false`)
Expected: `200`, aucune erreur dans les logs liée à `openclaw`.

- [ ] **Step 7: Commit**

```bash
git add src/jarvis/interfaces/openclaw/setup.py src/jarvis/app.py tests/test_openclaw_setup.py
git commit -m "feat(openclaw): wiring setup_openclaw() dans le lifespan de app.py"
```

---

### Task 7: `VoiceCallTool` — appel sortant + wiring bootstrap

**Files:**
- Create: `src/jarvis/capabilities/tools/openclaw_voice.py`
- Modify: `src/jarvis/bootstrap.py` (instanciation `OpenClawClient` + `tool_registry.register(...)`, section proche de la ligne 365 `tool_registry.register(ReportMissingCapabilityTool(...))`)
- Test: `tests/test_openclaw_voice_tool.py`

**Interfaces:**
- Consumes : `OpenClawClientProtocol` (Task 2) — constructeur `VoiceCallTool(client: OpenClawClientProtocol)`.
- Produces : `Tool` conforme à `capabilities/tools/base.py:17` (attributs `name`, `description`, `input_schema`, méthode `async execute(**kwargs) -> ToolResult`).
- RPC utilisée : `voicecall.initiate` (confirmée dans la doc Gateway RPC — fallback sur `toNumber` de config si `to` omis), `side_effect=True`.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_openclaw_voice_tool.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.capabilities.tools.openclaw_voice import VoiceCallTool


@pytest.mark.asyncio
async def test_execute_appelle_voicecall_initiate_en_side_effect() -> None:
    client = MagicMock(request=AsyncMock(return_value={"callId": "call-1"}))
    tool = VoiceCallTool(client=client)

    result = tool.to_claude_schema()
    assert result["name"] == "voice_call"

    result = await tool.execute(to="+15145551234", message="Rappel : rendez-vous à 15h")

    assert result.is_error is False
    client.request.assert_awaited_once()
    args, kwargs = client.request.await_args
    assert args[0] == "voicecall.initiate"
    assert args[1]["to"] == "+15145551234"
    assert kwargs["side_effect"] is True


@pytest.mark.asyncio
async def test_execute_remonte_erreur_rpc_proprement() -> None:
    client = MagicMock(request=AsyncMock(side_effect=RuntimeError("Gateway down")))
    tool = VoiceCallTool(client=client)

    result = await tool.execute(to="+15145551234")

    assert result.is_error is True
    assert "Gateway down" in result.content
```

- [ ] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/test_openclaw_voice_tool.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implémenter**

```python
# src/jarvis/capabilities/tools/openclaw_voice.py
from __future__ import annotations

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.kernel.contracts import OpenClawClientProtocol


class VoiceCallTool(Tool):
    name = "voice_call"
    description = (
        "Passe un appel téléphonique sortant via le Gateway OpenClaw (Twilio). "
        "Utilise-le pour rappeler l'utilisateur ou transmettre un message vocal."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Numéro E.164, ex. +15145551234"},
            "message": {"type": "string", "description": "Message à énoncer à la prise d'appel"},
            "mode": {"type": "string", "enum": ["notify", "conversation"], "default": "notify"},
        },
        "required": ["to"],
    }

    def __init__(self, client: OpenClawClientProtocol) -> None:
        self._client = client

    async def execute(self, **kwargs: object) -> ToolResult:
        try:
            payload = {
                "to": kwargs["to"],
                "message": kwargs.get("message", ""),
                "mode": kwargs.get("mode", "notify"),
            }
            result = await self._client.request("voicecall.initiate", payload, side_effect=True)
            return ToolResult(content=f"Appel déclenché ({result.get('callId', '?')}).")
        except Exception as exc:  # noqa: BLE001 — remonté tel quel au LLM comme erreur outil
            return ToolResult(content=f"Échec de l'appel : {exc}", is_error=True)
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `uv run pytest tests/test_openclaw_voice_tool.py -v`
Expected: PASS

- [ ] **Step 5: Wiring bootstrap — client unique + enregistrement conditionnel**

```python
# src/jarvis/bootstrap.py — près de la ligne 365 (après ReportMissingCapabilityTool), ajouter :
    if settings.openclaw_enabled and os.getenv("OPENCLAW_VOICE_ENABLED", "false").lower() == "true":
        from jarvis.capabilities.tools.openclaw_voice import VoiceCallTool
        from jarvis.providers.openclaw.client import OpenClawClient

        openclaw_client = OpenClawClient(
            ws_url=os.environ["OPENCLAW_WS_URL"], token=os.environ["OPENCLAW_GATEWAY_TOKEN"]
        )
        tool_registry.register(VoiceCallTool(client=openclaw_client))
```

Vérifier au préalable si `settings.openclaw_enabled` existe déjà dans `kernel/settings.py` — sinon utiliser uniquement `os.getenv("OPENCLAW_ENABLED", "false").lower() == "true"` (pattern déjà utilisé pour les autres flags de canaux, cf. `interfaces/channels/setup.py`) :

Run: `uv run python -c "from jarvis.kernel.settings import settings; print(hasattr(settings, 'openclaw_enabled'))"`
Expected : si `False`, remplacer la condition par le seul `os.getenv(...)`.

- [ ] **Step 6: Vérifier `lint-imports` et la non-régression du smoke test**

Run: `uv run lint-imports && uv run python scripts/validation/smoke_runtime.py`
Expected: vert, y compris avec `OPENCLAW_VOICE_ENABLED` absent (le tool ne s'enregistre pas, aucun import levé au chargement).

- [ ] **Step 7: Commit**

```bash
git add src/jarvis/capabilities/tools/openclaw_voice.py src/jarvis/bootstrap.py tests/test_openclaw_voice_tool.py
git commit -m "feat(openclaw): VoiceCallTool (appel sortant) + wiring bootstrap"
```

---

### Task 8: Configuration OpenClaw — plugin voice-call en mode realtime

**Files:**
- Create: `config/openclaw.json` (JSON5, versionné SANS secrets)
- Create: `docs/openclaw.md` (procédure Console Twilio + ngrok + lancement WSL2, référencée par les briques précédentes)

Ce n'est pas du code Python — pas de cycle TDD, mais une vérification de bout en bout obligatoire.

- [ ] **Step 1: Écrire `config/openclaw.json`**

```json5
{
  gateway: { bind: "127.0.0.1", port: 18789 },
  agents: {
    list: [
      {
        id: "jarvis",
        workspace: "./workspace-jarvis",
        runtime: { type: "acp", acp: { agent: "jarvis-acp", backend: "acpx", mode: "persistent" } },
      },
    ],
  },
  plugins: {
    entries: {
      acpx: {
        enabled: true,
        config: {
          agents: {
            "jarvis-acp": {
              command: "bin/jarvis-acp-launch.sh",
            },
          },
        },
      },
      "voice-call": {
        enabled: true,
        config: {
          twilio: {
            accountSid: "${TWILIO_ACCOUNT_SID}",
            authToken: { secretRef: "twilio-auth-token" },
            fromNumber: "${OPENCLAW_VOICE_NUMBER}",
          },
          publicUrl: "${PUBLIC_BASE_URL}",
          realtime: {
            enabled: true,
            provider: "google",
            providers: {
              google: {
                apiKey: { secretRef: "gemini-api-key" },
                model: "gemini-3.1-flash-live-preview",
                speakerVoice: "Kore",
                silenceDurationMs: 500,
                startSensitivity: "high",
              },
            },
            agentContext: {
              enabled: true,
              maxChars: 6000,
              includeIdentity: true,
              files: ["SOUL.md", "IDENTITY.md", "USER.md"],
            },
            // "provider-direct" (défaut) : le modèle realtime répond seul aux tours
            // triviaux et n'appelle openclaw_agent_consult (→ Jarvis) que pour les
            // décisions/outils/mémoire — c'est la Stratégie A. Ne PAS mettre
            // "force-agent-consult" ici : ça routerait chaque tour vers Jarvis et
            // annulerait le gain de latence recherché.
            consultRouting: "provider-direct",
          },
        },
      },
    },
  },
  bindings: [{ type: "acp", agentId: "jarvis", match: { channel: "voice-call" } }],
}
```

- [ ] **Step 2: Écrire le wrapper de lancement du harnais (acpx ne supporte que `command`/`args`, pas `env`)**

```bash
#!/usr/bin/env bash
# bin/jarvis-acp-launch.sh
set -euo pipefail
export JARVIS_API_URL="${JARVIS_API_URL:-http://127.0.0.1:8000}"
export JARVIS_API_TOKEN="${JARVIS_API_TOKEN:?JARVIS_API_TOKEN doit être défini dans l'environnement du Gateway}"
export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src:${PYTHONPATH:-}"
exec python3 -m jarvis.interfaces.openclaw.acp_harness
```

Run: `chmod +x bin/jarvis-acp-launch.sh`

- [ ] **Step 3: Documenter la procédure dans `docs/openclaw.md`**

Contenu minimal requis (à rédiger, pas de placeholder — chaque point doit citer la commande/URL réelle une fois exécutée) :
1. Lancement du Gateway en WSL2 (`openclaw gateway`, avec `OPENCLAW_CONFIG_PATH` pointé sur `config/openclaw.json`).
2. Génération du token Gateway (commande CLI exacte relevée à l'exécution) → valeur mise dans `.env` (`OPENCLAW_GATEWAY_TOKEN`).
3. Déclaration des SecretRefs `twilio-auth-token` et `gemini-api-key` (commande `openclaw secrets set ...` — syntaxe exacte à relever dans `docs.openclaw.ai/gateway/configuration` au moment de l'exécution).
4. Configuration webhook voix Twilio Console → URL exposée par le plugin voice-call (relevée dans les logs du Gateway au démarrage).
5. Tunnel ngrok additionnel vers le port webhook du plugin (distinct du tunnel déjà utilisé pour `PUBLIC_BASE_URL`).

- [ ] **Step 4: Vérification E2E**

1. `OPENCLAW_ENABLED=true OPENCLAW_VOICE_ENABLED=true uv run python -m jarvis.app` (API Jarvis).
2. `openclaw gateway` dans WSL2 avec la config ci-dessus.
3. `curl -H "Authorization: Bearer $JARVIS_API_TOKEN" http://127.0.0.1:8000/api/openclaw/health` → `{"connected": true}`.
4. Appeler le numéro Twilio dédié → le modèle realtime décroche ; poser une question nécessitant la mémoire de Jarvis (ex. "rappelle-moi ce qu'on a dit hier") → vérifier dans les logs FastAPI un `POST /api/openclaw/message` avec `meta.channel="voice-call"`, traité par `voice_gateway`.
5. Mesurer la latence : `openclaw voicecall latency --last 5` → comparer aux seuils de l'arbitrage (p90 < 2 s visé pour les tours consultés).
6. Depuis WebChat ou Telegram, demander « appelle-moi au +1514... » → `voice_call` déclenche l'appel sortant.

- [ ] **Step 5: Commit**

```bash
git add config/openclaw.json bin/jarvis-acp-launch.sh docs/openclaw.md
git commit -m "feat(openclaw): config voice-call realtime + binding jarvis + doc procédure"
```

---

## Self-Review (effectuée)

1. **Couverture** : client WS (T1) → contrat kernel (T2) → sessions (T3) → endpoint texte+voix avec route rapide (T4) → harnais ACP (T5) → wiring boot (T6) → outbound tool (T7) → config Gateway + Twilio (T8). Couvre l'intégralité de la Stratégie A (realtime + `openclaw_agent_consult` + `agentContext`) et le socle minimal de la Stratégie B (route rapide via `voice_gateway` existant). Le streaming ACP fin (chunks `session/update` multiples) et la route STT/TTS avancée (Deepgram Flux, mulaw natif) restent des optimisations ultérieures, hors scope de ce premier plan — à ouvrir une fois les mesures de T8 disponibles.
2. **Placeholders** : aucun — chaque étape de code contient l'implémentation réelle ; seule la Task 8 (config/doc) documente explicitement les commandes dont la syntaxe exacte doit être relevée à l'exécution (tokens, secrets CLI), ce qui est un point de vérification opérationnel, pas du code manquant.
3. **Cohérence des types** : `OpenClawClientProtocol.request(method, params, *, side_effect, timeout)` identique entre `client.py` (T1), `contracts.py` (T2) et son usage dans `openclaw_voice.py` (T7). `Gateway.handle(message, session_id, stream)` utilisé avec les mêmes noms de paramètres qu'en T4 et dans le code existant de `engine/gateway.py:59`.

## Handoff d'exécution

Plan complet, sauvegardé dans `docs/architecture/2026-07-16-implementation-telephonie-twilio-realtime.md`. Deux options d'exécution :

1. **Pilotée par sous-agents (recommandé)** — un sous-agent frais par tâche, revue entre chaque tâche.
2. **Exécution en ligne** — tâches exécutées dans cette session, par lots avec points de contrôle.

Quelle approche ?
