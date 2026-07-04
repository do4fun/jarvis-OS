# Intégration Twilio multicanale — Plan d'architecture et d'implémentation

> Voix (Media Streams WebSocket bidirectionnel) + WhatsApp + Facebook Messenger,
> centralisés dans le cerveau Jarvis (`Gateway.handle`) avec contexte unifié par utilisateur.

---

## 1. Architecture globale

```
                                   ┌──────────────────────── TWILIO CLOUD ────────────────────────┐
                                   │                                                              │
  ☎ PSTN  ──── appel entrant ────▶ │  Numéro Twilio                                               │
  ☎ PSTN  ◀─── appel sortant ───── │  (Voice + Messaging)          WhatsApp API      Messenger    │
                                   └──────┬──────────┬──────────────────┬────────────────┬────────┘
                                          │          │                  │                │
                     POST /api/twilio/    │          │ WSS (Media       │ POST form-url  │ POST form-url
                     voice/incoming       │          │ Streams, μ-law   │ encoded        │ encoded
                     (TwiML <Connect>)    │          │ 8 kHz, base64)   │                │
                                          ▼          ▼                  ▼                ▼
┌──────────────────────────────────────── SERVEUR JARVIS (FastAPI, HTTPS public) ──────────────────────────┐
│                                                                                                          │
│  interfaces/api/twilio.py                 interfaces/voice/twilio/          interfaces/channels/         │
│  ├── POST /api/twilio/voice/incoming      ├── media_stream.py               ├── twilio_messaging.py      │
│  │     → TwiML <Connect><Stream>          │    WS /ws/twilio/media          │    TwilioMessagingChannel  │
│  ├── POST /api/twilio/messaging/webhook   │    (bidirectionnel)             │    (whatsapp + messenger)  │
│  │     → validation signature             ├── stt_stream.py                 │                            │
│  │     → TwilioMessagingChannel           │    Deepgram live (mulaw 8k)     │  gateway.py                │
│  └── POST /api/twilio/voice/status        └── audio.py                      │    MessagingGateway        │
│        (callbacks fin d'appel)                 transcodage ⇄ μ-law 8 kHz    │    (dispatch existant)     │
│                                                                             └────────────┬───────────────┘
│                                                     │                                    │               │
│                    ┌────────────────────────────────┴────────────────────────────────────┘               │
│                    ▼                                                                                     │
│  ═══════════ CERVEAU (existant, inchangé) ═══════════                                                    │
│  engine/gateway.py :  Gateway.handle(message, session_id, stream)                                        │
│      → Session + RouteEnum + réponse (str | AsyncIterator[str])                                          │
│  SessionKeyStore : "twilio:+15145551234"  →  session_id UUID  (partagé voix/écrit)                       │
│  Memory Kernel, tools, consolidation : déjà branchés via Gateway                                         │
│                                                                                                          │
│  capabilities/tools/phone_call.py : PhoneCallTool (Jarvis initie des appels sortants)                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Principe directeur** : rien ne contourne `Gateway.handle`. La voix et l'écrit sont
des *interfaces L3* qui produisent du texte entrant et consomment du texte sortant.
Le contexte unifié vient d'une clé de session commune basée sur le numéro E.164.

### Flux voix entrant (temps réel)

```
Appel entrant → Twilio → POST /api/twilio/voice/incoming
  → réponse TwiML : <Connect><Stream url="wss://HOST/ws/twilio/media">
                      <Parameter name="caller" value="{{From}}"/></Stream></Connect>
  → Twilio ouvre le WS → événements JSON : connected, start, media (μ-law b64), stop
  → media → Deepgram live (encoding=mulaw&sample_rate=8000) → transcripts
  → transcript final → Gateway.handle(texte, session_id, stream=False)
  → réponse → tts_engine.synthesize() → WAV → resample+μ-law → frames "media" vers Twilio
  → barge-in : nouvelle parole pendant la lecture → événement "clear" (flush du buffer Twilio)
```

### Flux écrit (WhatsApp / Messenger)

```
Message → Twilio → POST /api/twilio/messaging/webhook (form-urlencoded)
  → validation X-Twilio-Signature
  → IncomingMessage(platform, user_id=From, text=Body)
  → MessagingGateway.dispatch() (existant) → Gateway.handle → réponse
  → TwilioMessagingChannel.send() → client.messages.create(from_, to, body)
```

---

## 2. Configuration console Twilio

### 2.1 Prérequis compte

| Étape | Détail |
|---|---|
| Compte + numéro | Acheter un numéro Voice+SMS (E.164). Noter `ACCOUNT_SID` / `AUTH_TOKEN` (Console → Account Info). |
| URL publique HTTPS | Dev : `ngrok http 8000` (ou Tailscale Funnel). Prod : reverse proxy TLS (Caddy/nginx). Twilio exige HTTPS/WSS valide. |

### 2.2 Voix (Media Streams)

1. Console → Phone Numbers → ton numéro → **Voice Configuration** :
   - *A call comes in* → Webhook → `https://HOST/api/twilio/voice/incoming` (HTTP POST)
   - *Call status changes* → `https://HOST/api/twilio/voice/status` (POST)
2. Aucune config supplémentaire pour Media Streams : le `<Connect><Stream>` du TwiML retourné suffit. Le flux est **bidirectionnel** par défaut avec `<Connect>` (contrairement à `<Start><Stream>` qui est unidirectionnel écoute seule).
3. Appels sortants : rien à configurer, ils passent par l'API REST (`client.calls.create`) avec le même TwiML.

### 2.3 WhatsApp

1. **Dev — Sandbox** : Console → Messaging → Try it out → WhatsApp sandbox. Envoyer `join <code>` au numéro sandbox depuis ton téléphone.
   - *When a message comes in* → `https://HOST/api/twilio/messaging/webhook` (POST)
2. **Prod** : enregistrer un **WhatsApp Sender** (Messaging → Senders → WhatsApp) lié à un WhatsApp Business Account Meta (vérification d'entreprise requise, quelques jours). Puis même webhook sur le sender.
3. Format d'adresse : `whatsapp:+15145551234` (préfixe obligatoire dans `From`/`To`).

### 2.4 Facebook Messenger

1. Console → Messaging → Channels → **Facebook Messenger** → connecter la Page Facebook (OAuth Meta, il faut être admin de la page).
2. Sur le sender Messenger créé : *When a message comes in* → même webhook `https://HOST/api/twilio/messaging/webhook`.
3. Format d'adresse : `messenger:<PSID>` pour l'utilisateur, `messenger:<PAGE_ID>` pour la page. ⚠️ Le PSID est un identifiant opaque par page — pas de numéro de téléphone (impact sur l'unification d'identité, voir §5).

---

## 3. Structure serveur & code d'infrastructure

### 3.1 Arborescence (greffée sur l'existant)

```
src/jarvis/
├── interfaces/
│   ├── api/
│   │   └── twilio.py                  # NOUVEAU — routes REST (TwiML voix, webhook messaging, status)
│   ├── channels/
│   │   ├── twilio_messaging.py        # NOUVEAU — TwilioMessagingChannel (remplace le stub whatsapp.py)
│   │   ├── base.py                    # existant — ChannelAdapter, IncomingMessage, MessageTarget
│   │   ├── gateway.py                 # existant — MessagingGateway (dispatch + session_map)
│   │   └── setup.py                   # MODIFIÉ — branche TWILIO_ENABLED
│   └── voice/
│       └── twilio/
│           ├── __init__.py
│           ├── media_stream.py        # NOUVEAU — handler WS /ws/twilio/media
│           ├── stt_stream.py          # NOUVEAU — bridge Deepgram live streaming
│           └── audio.py               # NOUVEAU — transcodage WAV/MP3 ⇄ μ-law 8 kHz
├── capabilities/tools/
│   └── phone_call.py                  # NOUVEAU — PhoneCallTool (appels sortants par commande vocale)
└── kernel/
    └── settings.py                    # MODIFIÉ — champs twilio_*
```

Dépendance : `twilio` (SDK officiel, léger) dans `pyproject.toml`. Deepgram live : WS brut via la lib `websockets` déjà présente (pas de SDK supplémentaire).

### 3.2 Settings (`kernel/settings.py`)

```python
# ── Twilio ────────────────────────────────────────────────
twilio_enabled: bool = Field(default=False)
twilio_account_sid: str = Field(default="")
twilio_auth_token: SecretStr = Field(default=SecretStr(""))
twilio_phone_number: str = Field(default="", description="Numéro voix E.164, ex +15145551234")
twilio_whatsapp_number: str = Field(default="", description="Sender WhatsApp, ex whatsapp:+14155238886")
twilio_messenger_page_id: str = Field(default="", description="messenger:<PAGE_ID>")
public_base_url: str = Field(default="", description="URL HTTPS publique (ngrok/proxy) pour TwiML et webhooks")
twilio_validate_signature: bool = Field(default=True)
```

### 3.3 Routes REST + webhook écrit (`interfaces/api/twilio.py`)

```python
from fastapi import APIRouter, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Connect

router = APIRouter()


async def _validate(request: Request, form: dict) -> bool:
    if not settings.twilio_validate_signature:
        return True
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    signature = request.headers.get("X-Twilio-Signature", "")
    # URL telle que Twilio l'a signée = URL publique, pas l'URL interne du proxy
    url = f"{settings.public_base_url}{request.url.path}"
    return validator.validate(url, form, signature)


@router.post("/api/twilio/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    form = dict(await request.form())
    if not await _validate(request, form):
        return Response(status_code=403)
    ws_url = settings.public_base_url.replace("https://", "wss://") + "/ws/twilio/media"
    vr = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=ws_url)
    stream.parameter(name="caller", value=form.get("From", ""))   # propagé dans l'événement WS "start"
    vr.append(connect)
    return Response(content=str(vr), media_type="application/xml")


@router.post("/api/twilio/messaging/webhook")
async def messaging_webhook(request: Request) -> Response:
    form = dict(await request.form())                # ⚠️ form-urlencoded, PAS json
    if not await _validate(request, form):
        return Response(status_code=403)
    channel = request.app.state.twilio_messaging     # TwilioMessagingChannel
    gateway = request.app.state.messaging_gateway    # MessagingGateway existant
    msg = channel.parse_webhook(form)                # → IncomingMessage
    import asyncio
    asyncio.create_task(gateway.dispatch(msg))       # répondre 200 vite (timeout Twilio 15 s)
    return Response(content="<Response/>", media_type="application/xml")
```

> **Pourquoi une route dédiée** et pas le `POST /api/channels/{platform}/webhook` existant :
> ce handler fait `await request.json()` or Twilio POSTe en `application/x-www-form-urlencoded`,
> et la validation de signature est spécifique Twilio. On garde le routeur générique intact.

### 3.4 Channel écrit (`interfaces/channels/twilio_messaging.py`)

Un seul adapter pour les deux plateformes — Twilio les distingue par le préfixe d'adresse
(`whatsapp:` / `messenger:`), l'API `messages.create` est identique.

```python
from twilio.rest import Client
from jarvis.interfaces.channels.base import ChannelAdapter, IncomingMessage, MessageTarget, Platform


class TwilioMessagingChannel(ChannelAdapter):
    platform = Platform.WHATSAPP        # plateforme principale ; messenger routé par channel_id

    def __init__(self) -> None:
        super().__init__()
        self._client = Client(settings.twilio_account_sid,
                              settings.twilio_auth_token.get_secret_value())

    async def start(self) -> None: ...      # webhook push : rien à démarrer
    async def stop(self) -> None: ...

    def parse_webhook(self, form: dict) -> IncomingMessage:
        sender = form.get("From", "")       # "whatsapp:+1514..." ou "messenger:1234567"
        platform = Platform.WHATSAPP if sender.startswith("whatsapp:") else Platform.MESSENGER
        return IncomingMessage(
            platform=platform,
            user_id=sender,                 # adresse complète préfixée = user_id
            text=form.get("Body", ""),
            raw=form,
        )

    async def send(self, reply: str, target: MessageTarget) -> None:
        from_addr = (settings.twilio_whatsapp_number
                     if target.user_id.startswith("whatsapp:")
                     else f"messenger:{settings.twilio_messenger_page_id}")
        import asyncio
        await asyncio.to_thread(                      # SDK Twilio synchrone → thread
            self._client.messages.create,
            from_=from_addr, to=target.user_id, body=reply,
        )
```

Câblage dans `setup_channels()` : si `settings.twilio_enabled`, instancier, `messaging_gw.register(...)`,
`app.state.twilio_messaging = channel`, inclure `twilio_router`. (Ajouter `MESSENGER` à l'enum `Platform`.)

### 3.5 Handler WebSocket Media Streams (`interfaces/voice/twilio/media_stream.py`)

Protocole Twilio : frames JSON texte sur le WS. Entrant : `connected` → `start` (streamSid,
customParameters) → `media` (payload = μ-law 8 kHz base64, ~20 ms/frame) → `stop`.
Sortant : `media` (même format), `mark` (ack de lecture), `clear` (flush = barge-in).

```python
import asyncio, base64, json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/twilio/media")
async def twilio_media(ws: WebSocket) -> None:
    await ws.accept()
    stream_sid: str | None = None
    session_id: str | None = None
    gateway = ws.app.state.voice_gateway
    stt = DeepgramLiveSTT()                 # stt_stream.py — WS Deepgram mulaw/8000/fr
    speaking = asyncio.Event()              # True pendant l'envoi TTS (barge-in)

    async def on_transcript(text: str) -> None:
        nonlocal session_id
        if speaking.is_set():                                    # barge-in
            await ws.send_json({"event": "clear", "streamSid": stream_sid})
            speaking.clear()
        session, _route, response = await gateway.handle(text, session_id=session_id, stream=False)
        session_id = str(session.id)
        wav = await tts_engine.synthesize(str(response))
        speaking.set()
        for frame in wav_to_mulaw_frames(wav):                   # audio.py — chunks 20 ms
            if not speaking.is_set():
                break                                            # interrompu
            await ws.send_json({"event": "media", "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(frame).decode()}})
        await ws.send_json({"event": "mark", "streamSid": stream_sid,
                            "mark": {"name": "eot"}})
        speaking.clear()

    await stt.connect(on_final=on_transcript)
    try:
        while True:
            frame = json.loads(await ws.receive_text())
            match frame["event"]:
                case "start":
                    stream_sid = frame["start"]["streamSid"]
                    caller = frame["start"]["customParameters"].get("caller", "")
                    session_id = session_key_store.resolve(f"twilio:{caller}")   # §5
                case "media":
                    await stt.send(base64.b64decode(frame["media"]["payload"]))
                case "stop":
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await stt.close()
        if session_id and stream_sid:
            session_key_store.persist(f"twilio:{caller}", session_id)
```

### 3.6 Bridge STT streaming (`stt_stream.py`) — Deepgram live

`DEEPGRAM_API_KEY` est déjà dans le `.env` (utilisé par le pipeline LiveKit). WS brut :

```
wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000&channels=1
    &language=fr&model=nova-2&interim_results=true&endpointing=300&smart_format=true
Header : Authorization: Token <DEEPGRAM_API_KEY>
```

- On pousse le μ-law brut **tel quel** (Deepgram le décode nativement — zéro transcodage entrant).
- On déclenche `on_final` quand `is_final=true` **et** `speech_final=true` (fin d'énoncé).
- Un `interim` reçu pendant `speaking.is_set()` = détection de barge-in anticipée (optionnel v2).
- Fallback sans clé Deepgram : buffer μ-law → PCM float32 16 kHz → `providers/audio/stt.transcribe()`
  (faster-whisper local) par fenêtres VAD. Latence supérieure, mais 100 % local.

### 3.7 Transcodage audio (`audio.py`)

`tts_engine.synthesize()` retourne du **WAV** (Piper : sample rate du modèle ; Gemini : PCM 24 kHz)
ou du **MP3** (ElevenLabs). Twilio exige μ-law 8 kHz mono.

```python
import audioop, wave, io

def wav_to_mulaw_frames(wav_bytes: bytes, frame_ms: int = 20) -> Iterator[bytes]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        pcm, rate, width = w.readframes(w.getnframes()), w.getframerate(), w.getsampwidth()
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)      # resample → 8 kHz
    ulaw = audioop.lin2ulaw(pcm, 2)                           # PCM16 → μ-law
    n = 8000 * frame_ms // 1000                               # 160 bytes / 20 ms
    for i in range(0, len(ulaw), n):
        yield ulaw[i:i + n]
```

> ⚠️ `audioop` est retiré de Python 3.13. Le bundle est en 3.11 → OK aujourd'hui ;
> prévoir `audioop-lts` (backport PyPI) ou une implémentation numpy le jour de la migration.
> MP3 ElevenLabs : décoder via `pydub`/ffmpeg, ou forcer `output_format=pcm_16000` côté API ElevenLabs.

### 3.8 Appels sortants (`capabilities/tools/phone_call.py`)

`PhoneCallTool` enregistré dans `tool_registry` (bootstrap.py) → automatiquement dispo en vocal,
comme `LaunchAppTool`. Tier **confirm systématique** (action externe = niveau 5 CDC §10.1).

```python
call = client.calls.create(
    to=numero_e164,
    from_=settings.twilio_phone_number,
    twiml=f'<Response><Connect><Stream url="{ws_url}">'
          f'<Parameter name="caller" value="{numero_e164}"/></Stream></Connect></Response>',
)
```

L'appel sortant rejoint **le même handler WS** que l'entrant — pipeline unique. Un paramètre
custom `initial_intent` peut porter la consigne ("annonce à X que…") injectée comme premier
tour de conversation.

---

## 4. Cycle de vie & contexte unifié chat ⇄ téléphone

### 4.1 Clé d'identité

Le mécanisme existe déjà : `IncomingMessage.session_key = "{platform}:{user_id}"` mappé vers un
`session_id` UUID persisté (`memory/messaging_sessions.json` dans `MessagingGateway`).

**Extension** : extraire ce mapping dans un `SessionKeyStore` partagé (même fichier JSON),
utilisé par `MessagingGateway` (écrit) **et** `media_stream.py` (voix), avec **normalisation E.164** :

| Canal | Adresse brute | Clé normalisée |
|---|---|---|
| Appel voix | `+15145551234` | `twilio:+15145551234` |
| WhatsApp | `whatsapp:+15145551234` | `twilio:+15145551234` |
| Messenger | `messenger:98137602…` (PSID opaque) | `twilio:messenger:98137602…` |

→ Un utilisateur qui écrit sur WhatsApp puis appelle retombe sur **la même `Session`** : le
`Gateway` recharge l'historique, le Memory Kernel a déjà les faits — continuité native, zéro
code supplémentaire dans le cerveau.

**Cas Messenger** : pas de numéro → pas de fusion automatique. Table de liaison optionnelle
`identity_links.json` (`psid → e164`) alimentée manuellement ou par un mini-flux "donne-moi ton
numéro" à la première conversation. V1 : sessions Messenger séparées, assumé.

### 4.2 Cycle de vie d'un appel

| Moment | Action |
|---|---|
| `start` | résolution session, message système optionnel "conversation téléphonique" injecté |
| pendant | chaque énoncé final → tour de conversation standard (mémoire, tools, tout fonctionne) |
| `stop` / status callback | persistance du mapping, hooks existants (consolidation, auto_dream) sur les derniers tours, log durée/CallSid |
| silence > N s | relance TTS "tu es toujours là ?" puis raccrochage propre (`client.calls(sid).update(status="completed")`) |

---

## 5. Sécurité & scalabilité

### Sécurité

1. **Signature Twilio sur tous les webhooks HTTP** : `RequestValidator.validate(url_publique, form, X-Twilio-Signature)` — HMAC-SHA1 sur URL + params triés. Piège classique derrière un proxy : valider avec l'**URL publique** (`public_base_url`), pas l'URL interne. 403 sinon.
2. **WebSocket Media Streams** : Twilio ne signe pas les frames WS. Défense : (a) l'URL WS n'est communiquée que dans le TwiML d'une requête déjà validée, (b) ajouter un jeton éphémère en query string (`/ws/twilio/media?t=<jwt courte durée>` généré dans `voice_incoming`, vérifié à l'`accept()`), (c) vérifier `accountSid` dans l'événement `start`.
3. **Auth existante** : exclure `/api/twilio/*` et `/ws/twilio/*` de `verify_api_token` (comme les webhooks channels actuels) — la signature Twilio EST leur auth.
4. **Secrets** : `TWILIO_AUTH_TOKEN` en `SecretStr`, jamais loggué. Redaction du `Body` des messages dans les logs INFO (données personnelles).
5. **Anti-abus** : whitelist optionnelle de numéros appelants (`twilio_allowed_callers`) — un assistant personnel n'a pas vocation à répondre à n'importe qui ; rejet TwiML `<Reject/>` sinon.

### Scalabilité

1. **Tout async, un WS par appel** : le handler est 100 % asyncio (Deepgram WS, Gateway, envoi de frames). Un process uvicorn tient facilement des dizaines d'appels simultanés — le goulot est le LLM, pas l'I/O.
2. **Cadence d'envoi** : Twilio bufferise, mais envoyer le TTS plus vite que temps réel remplit son buffer → `clear` (barge-in) devient laggy. Pacer l'envoi (~1 frame/20 ms via `asyncio.sleep`) après un burst initial de ~1 s.
3. **Semaphore LLM** : `asyncio.Semaphore(settings.max_concurrent_calls)` autour de `gateway.handle` côté voix, pour borner coût et latence sous charge.
4. **Multi-instance (plus tard)** : le WS est stateful par appel mais autonome (aucun état partagé en RAM hors `SessionKeyStore`). Load-balancer L7 avec affinité par connexion suffit ; le mapping sessions doit alors migrer JSON → SQLite/Redis.
5. **Timeouts** : Media Streams coupe après ~10 min de silence total ; webhook messaging doit répondre < 15 s → d'où le `202-style` (retour `<Response/>` immédiat + `asyncio.create_task(dispatch)`).

---

## 6. Plan d'action étape par étape

| # | Étape | Livrable | Validation |
|---|---|---|---|
| 0 | Compte Twilio, numéro, `ngrok http 8000`, `uv add twilio`, settings + `.env` | config | `client.api.accounts.get()` OK |
| 1 | **Écrit WhatsApp (sandbox)** : `twilio_messaging.py`, route webhook, `Platform.MESSENGER`, câblage `setup_channels` | messages bidirectionnels | envoyer "salut" au sandbox → réponse Jarvis avec mémoire |
| 2 | **Signature + hardening** : `RequestValidator`, exclusion auth, redaction logs | sécurité écrit | requête forgée sans signature → 403 |
| 3 | **Messenger** : connexion page FB dans la console, test PSID | 2ᵉ canal écrit | message page FB → réponse |
| 4 | **Voix entrante** : `audio.py` (+ tests unitaires transcodage), `stt_stream.py`, `media_stream.py`, route TwiML | appel entrant conversationnel | appeler le numéro, dialoguer, vérifier latence < ~2 s/tour |
| 5 | **Barge-in + fins d'appel propres** : `clear`, pacing, silence timeout, status callback | robustesse | couper la parole à Jarvis → il s'arrête |
| 6 | **Voix sortante** : `PhoneCallTool` (tier confirm) + `initial_intent` | Jarvis appelle | "Jarvis, appelle X et dis-lui…" → appel + annonce |
| 7 | **Contexte unifié** : `SessionKeyStore` partagé, normalisation E.164 | continuité chat⇄tél | écrire sur WhatsApp, appeler, vérifier que Jarvis se souvient |
| 8 | **Prod** : sender WhatsApp officiel, proxy TLS permanent, whitelist appelants, semaphore | mise en service | suite pytest + appel de bout en bout |

**Ordre volontaire** : l'écrit d'abord (peu de code, valide toute la chaîne webhook/signature/dispatch),
la voix ensuite (la complexité audio s'appuie sur une fondation déjà testée).

---

## 7. Risques & décisions assumées

| Risque | Mitigation |
|---|---|
| `audioop` retiré en Python 3.13 | bundle en 3.11 ; `audioop-lts` le jour venu |
| Format TTS non uniforme (WAV vs MP3 ElevenLabs) | normalisation dans `audio.py` ; forcer PCM côté ElevenLabs |
| Latence LLM sur appel téléphonique | `voice_gateway` (prompts courts) + Haiku ; filler audio "hmm" optionnel pendant la génération |
| Messenger sans identité téléphonique | sessions séparées en V1, table de liaison en V2 |
| Coûts Twilio (voix $/min, WhatsApp conv.) | BudgetGuard existant : ajouter un tracker d'usage Twilio (status callbacks donnent la durée) |
