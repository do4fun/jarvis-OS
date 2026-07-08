# Intégration du Gateway OpenClaw dans jarvis-OS — plan « bric par bric »

## Contexte

jarvis-OS est un assistant vocal/textuel Python (FastAPI + agent LiveKit, monolithe en couches strictes kernel → providers/capabilities → engine → interfaces, vérifié par import-linter). L'objectif : mettre le **Gateway OpenClaw** (démon Node.js, hub multi-canaux : WebChat, Telegram, Discord, SMS/voix Twilio, WhatsApp, iMessage…) **devant** Jarvis pour router les messages entrants de tous les canaux vers lui, et donner à Jarvis accès aux outils du Gateway (appels téléphoniques, SMS, etc.) — progressivement, une brique à la fois.

**Décisions utilisateur (validées)** :
1. **Jarvis reste le cerveau** — le Gateway est hub de canaux + routeur, pas un second cerveau.
2. **Première brique fonctionnelle : la voix** (appels téléphoniques).
3. **Coexistence puis retrait** : le MessagingGateway interne (`src/jarvis/interfaces/channels/`) reste en fallback, canal par canal, derrière des flags `.env`.

**Faits clés découverts** :
- Le mécanisme natif d'OpenClaw pour brancher un cerveau externe est **ACP** (Agent Client Protocol, JSON-RPC sur stdio) : `agents.list[].runtime = {type:"acp", acp:{agent:"jarvis-acp", backend:"acpx", mode:"persistent"}}` + adapter custom acpx. Les `bindings[]` routent chaque canal vers cet agent.
- « OpenClaw » existe déjà dans le repo comme vocabulaire de **skills** (ClawHub, `capabilities/skills/_mapping.py::resolve_tool`) — sans rapport avec le Gateway, mais réutilisable en Brique 5 (pont MCP).
- Les anciens tools Twilio (`phone_call`, `twilio_sms`…) vivent sur la branche **non mergée** `feature/twilio-multicanal` — ils ne sont PAS sur cette branche et ne seront pas portés (remplacés par les RPC du Gateway).
- **Facebook Messenger n'est pas un canal OpenClaw** — hors périmètre (alternative future : la beta Messenger de Twilio en direct, ou un plugin custom).
- OpenClaw cible macOS/Linux → sur ce host Windows 11, déploiement **Docker** (le repo a déjà `docker-compose.yml`).

## Conventions transverses

- **Anti-collision de nom** : `engine/gateway.py::Gateway` existe déjà. Tout ce qui touche OpenClaw s'appelle `openclaw` : `OpenClawClient`, `providers/openclaw/`, `interfaces/openclaw/`, routes `/api/openclaw/*`. Jamais « Gateway » nu.
- **Couches (import-linter)** : client WS → `providers/openclaw/client.py` (provider L1) ; Protocol `OpenClawClientProtocol` → `kernel/contracts.py` (les tools typent dessus, `bootstrap.build()` injecte — pattern DI existant) ; tools → `capabilities/tools/openclaw_*.py` ; harnais ACP + endpoint → `interfaces/`.
- **Flux ACP synchrone v1** : `session/prompt` → POST HTTP vers Jarvis (Bearer, timeout 300 s) → réponse en un bloc + `end_turn`. Streaming = Brique 5 (parité avec `MessagingGateway.dispatch` qui fait déjà `stream=False`).
- **Sessions** : clé `openclaw:{acp_session_id}` → mapping `memory/openclaw_sessions.json` (copie du pattern `_session_map` de `interfaces/channels/gateway.py`).
- **Une seule instance** `OpenClawClient`, créée dans `bootstrap.build()`, partagée (tools + setup interface) via le Container.
- **Dépendance** : ajouter `websockets>=13.0` explicitement dans `pyproject.toml` (aujourd'hui seulement transitif via uvicorn).
- **On ne construit PAS** : de nouveau ChannelAdapter, de portage des tools de `feature/twilio-multicanal`, de voix realtime (court-circuite le cerveau), de pont MCP avant B5, de suppression de `interfaces/channels/` avant fin B4.

## Brique 0 — Socle : Gateway en Docker + client Python + health-check

**But** : le Gateway tourne, Jarvis lui parle en WS authentifié, vérifiable au curl. Aucun canal ni agent.

1. `docker/openclaw/Dockerfile` (nouveau) : `node:22-slim` + `npm i -g openclaw@<version épinglée>` + `python3.11` + `httpx` (pour la Brique 1).
2. `docker-compose.yml` : service `openclaw` — `ports: ["127.0.0.1:18789:18789"]`, volumes `openclaw-config:/root/.openclaw`, `./config/openclaw.json:...:ro`, `./src:/opt/jarvis-src:ro`, `extra_hosts: host.docker.internal:host-gateway`, `env_file: .env`.
3. `config/openclaw.json` (nouveau, JSON5, versionné SANS secrets — token via env `OPENCLAW_GATEWAY_TOKEN`) : `{gateway:{bind:"0.0.0.0",port:18789}, agents:{list:[]}, bindings:[], channels:{}}`.
4. `src/jarvis/providers/openclaw/client.py` : `OpenClawClient` — `connect()` (1re frame `connect` + token, reconnexion backoff), `request(method, params, side_effect=False)` (corrélation req/res, `idempotencyKey` uuid4 si side-effect), `on_event()`, `health()`.
5. `src/jarvis/kernel/contracts.py` : `OpenClawClientProtocol`.
6. `src/jarvis/interfaces/openclaw/setup.py` : si `OPENCLAW_ENABLED=true`, connecte le client (tolérant si Gateway down), attache `app.state.openclaw_client`, monte le router — appelé du lifespan d'`app.py` (à côté de `setup_channels`) ; shutdown symétrique.
7. `src/jarvis/interfaces/api/openclaw.py` : `GET /api/openclaw/health` (derrière `verify_api_token`).

Flags : `OPENCLAW_ENABLED=false`, `OPENCLAW_WS_URL=ws://127.0.0.1:18789`, `OPENCLAW_GATEWAY_TOKEN=…` (+ `.env.example`).
Tests : `tests/unit/providers/test_openclaw_client.py` (serveur WS factice : handshake, corrélation, idempotency, events) ; `lint-imports` vert.
E2E : `docker compose up -d openclaw` → `curl …/api/openclaw/health` → `connected: true` ; mauvais token → refus logué.
Rollback : `OPENCLAW_ENABLED=false` + `docker compose stop openclaw`.

## Brique 1 — Jarvis cerveau ACP (validé via WebChat)

**But** : un message entrant sur un canal OpenClaw est routé (binding) vers l'agent « jarvis » = harnais ACP Python qui relaie vers `engine.Gateway.handle`. Le WebChat (canal core, zéro dépendance externe) sert de banc d'essai.

1. `src/jarvis/interfaces/openclaw/acp_harness.py` (~200 lignes, **stdlib + httpx uniquement**, pas de `bootstrap.build()`) : process stdio JSON-RPC — `initialize`, `session/new`, `session/prompt` → POST `{JARVIS_API_URL}/api/openclaw/message` `{text, external_key:"openclaw:"+session_id, meta:{channel,peer}}` → un `session/update` + `end_turn`, `session/cancel` → annule le httpx. Env : `JARVIS_API_URL` (défaut `http://host.docker.internal:8000`), `JARVIS_API_TOKEN`.
2. `src/jarvis/interfaces/api/openclaw.py` : `POST /api/openclaw/message` — auth Bearer, mapping `memory/openclaw_sessions.json`, `container.gateway.handle(text, session_id, stream=False)`, retour `{reply, session_id}` ; mapping périmé → nouvelle session.
3. `config/openclaw.json` : agent `jarvis` (runtime acp ci-dessus, `cwd:/opt/jarvis-src`) ; adapter acpx custom `jarvis-acp` → `python3 -m jarvis.interfaces.openclaw.acp_harness` avec `PYTHONPATH=/opt/jarvis-src` ; binding `{channel:"webchat", agent:"jarvis"}`.

Tests : dialogue stdio du harnais via subprocess + HTTP factice ; endpoint (réutilisation de session : 2 appels même `external_key` → même `session_id`, auth requise, persistance).
E2E : WebChat « Bonjour » → réponse du LLM Jarvis (logs FastAPI) ; 2e message → continuité mémoire ; Jarvis coupé → erreur propre sans crash du Gateway.
Rollback : retirer le binding webchat + restart conteneur.

## Brique 2 — Voix (priorité utilisateur) : plugin voice-call + Twilio

**But** : appels entrants/sortants via le plugin voice-call d'OpenClaw, cerveau = Jarvis via ACP. Le chemin LiveKit/SIP existant reste intact (coexistence).

- **Mode transcription retenu** (Deepgram STT → Jarvis via binding ACP → TTS ElevenLabs) : seul mode où chaque tour passe par Jarvis. Le mode realtime (Gemini Live/OpenAI) répond avec son propre modèle → exclu. `meta.channel="voice"` → consigne de brièveté.
- **Second numéro Twilio dédié** pendant la coexistence — ne pas toucher au numéro du trunk SIP LiveKit (`LIVEKIT_SIP_OUTBOUND_TRUNK_ID`).

1. `config/openclaw.json` : plugin voice-call (`provider:"twilio"`, creds via env `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`, `fromNumber`, `publicUrl` = ngrok `PUBLIC_BASE_URL`, transcription deepgram, TTS) + binding `{channel:"voice-call", agent:"jarvis"}`.
2. Console Twilio : webhook voix du numéro dédié → URL webhook du plugin ; ngrok routé aussi vers le port webhook du Gateway. Documenter dans `docs/openclaw.md`.
3. `src/jarvis/capabilities/tools/openclaw_voice.py` : `VoiceCallTool(Tool)` — `name="voice_call"`, schema `{to, message?, mode: notify|conversation}` → `client.request(<RPC outbound>, side_effect=True)` ; constructeur typé `OpenClawClientProtocol`.
4. `src/jarvis/bootstrap.py` : si `OPENCLAW_ENABLED` + `OPENCLAW_VOICE_ENABLED` → client unique dans le Container + `tool_registry.register(VoiceCallTool(...))` (auto-exposé au vocal LiveKit via `_build_voice_tools`).

Flags : `OPENCLAW_VOICE_ENABLED=false`, `OPENCLAW_VOICE_NUMBER=<second numéro>`.
Tests : tool avec client factice (params RPC, idempotency, erreur → `ToolResult(is_error=True)`).
E2E : appel entrant → réponse vocale générée par Jarvis (logs `POST /api/openclaw/message` meta voice) ; multi-tours ; outbound « appelle-moi au … » → le téléphone sonne ; **mesurer la latence de tour — si > ~4 s, ouvrir un chantier « route rapide voix » dans `engine.Gateway`**.
Rollback : `OPENCLAW_VOICE_ENABLED=false` + retirer binding/plugin + restart. LiveKit/SIP jamais touché.

## Brique 3 — SMS + WhatsApp (plugins Twilio OpenClaw)

1. `config/openclaw.json` : plugin twilio-sms (webhook via `PUBLIC_BASE_URL`) + canal WhatsApp (plugin officiel), bindings → `jarvis`.
2. Console Twilio : webhooks messaging du numéro SMS et du sender WhatsApp.
3. `src/jarvis/capabilities/tools/openclaw_messaging.py` : **un seul tool générique** `OpenClawSendTool` — `name="send_message"`, `{channel, to, text}` → RPC `chat.send`/envoi par canal, `side_effect=True` ; enregistré dans `bootstrap.py` sous `OPENCLAW_MESSAGING_TOOLS_ENABLED`.

Flags : `OPENCLAW_SMS_ENABLED` / `OPENCLAW_WHATSAPP_ENABLED` (documentent la bascule manuelle des bindings, pas de génération auto du JSON en v1) / `OPENCLAW_MESSAGING_TOOLS_ENABLED`.
E2E : SMS entrant → réponse Jarvis ; « envoie un SMS à X » → SMS reçu ; idem WhatsApp.
Rollback : retirer bindings + flags off. (Le stub WhatsApp interne n'a jamais été actif.)

## Brique 4 — Migration Telegram/Discord, retrait du MessagingGateway interne

Par canal, Telegram d'abord (seul canal interne pleinement fonctionnel) :
1. **Coexistence** : second bot Telegram (token distinct) branché sur OpenClaw, test parallèle.
2. **Bascule** : token principal déplacé dans openclaw.json ; `TELEGRAM_ENABLED=false` (le `setup_channels` existant gère — zéro code).
3. **Sessions** : script one-shot facultatif `scripts/migrate_channel_sessions.py` (`messaging_sessions.json` → `openclaw_sessions.json`) — sinon coupure acceptée.
4. Idem Discord. WebChat : déjà fait en B1.

**Critères de retrait par canal** : 7 j sans erreur de routage, continuité de session vérifiée, latence ≤ interne + 1 s, un rollback exercé.
**Retrait final** (tous canaux validés) : supprimer `src/jarvis/interfaces/channels/`, `src/jarvis/interfaces/api/channels.py`, l'appel `setup_channels` dans `app.py`, les flags associés, la dep `python-telegram-bot` ; archiver `memory/messaging_sessions.json`.

## Brique 5 (différée)

- **iMessage** : nécessite un Mac (BlueBubbles/imsg) — différé jusqu'à disponibilité d'un Mac.
- **Pont MCP** : exposer des tools Jarvis à OpenClaw via l'option MCP bridge d'acpx, en réutilisant `capabilities/skills/_mapping.py::resolve_tool`.
- **Streaming ACP** : chunks `session/update` + endpoint SSE (`stream=True`).
- **Autres canaux** : Slack/Teams/Signal (même recette que B3/B4). Messenger : uniquement via une voie custom (non-OpenClaw).

## Ordre & dépendances

```
B0 → B1 → B2 (voix, priorité)
        → B3 (SMS/WhatsApp)  → B4 (migration/retrait) → B5
```
B2 et B3 sont indépendantes entre elles.

## Vérification globale

- Après chaque brique : `pytest`, `lint-imports`, `ruff check`, puis le scénario E2E de la brique (ci-dessus).
- Smoke : `scripts/validation/smoke_runtime.py` reste vert avec `OPENCLAW_ENABLED=false` (prouve la réversibilité).

## Points à confirmer en début d'implémentation (risques)

1. Noms RPC exacts (health/statut, outbound du voice-call) — relever via `hello-ok.features.methods` au handshake.
2. Syntaxe exacte de l'enregistrement d'un adapter custom acpx (doc acpx / `docs.openclaw.ai/tools/acp-agents-setup`).
3. Chemins webhook exposés par voice-call et twilio-sms (config ngrok/Console Twilio).
4. Génération initiale du token Gateway dans le conteneur (`docker exec … openclaw …`).
5. Comportement du voice-call en mode transcription avec un agent ACP (latence réelle du tour de parole).
