# Intégration Twilio multicanale — Plan v2 (post-audit)

> Révision de [2026-06-28-twilio-multicanal.md](./2026-06-28-twilio-multicanal.md) intégrant les
> corrections de [2026-06-28-twilio-multicanal-debat.md](./2026-06-28-twilio-multicanal-debat.md).
> Changement structurant : la voix ne passe plus par un pont Media Streams maison mais par
> **Twilio Elastic SIP Trunking → LiveKit SIP**, qui réutilise tel quel le pipeline vocal existant
> (`interfaces/voice/agent.py`) au lieu d'en reconstruire un nouveau, plus lent et moins robuste.

---

## 1. Architecture globale

```
                              ┌──────────────── TWILIO CLOUD ────────────────┐
  ☎ PSTN ── appel entrant ──▶│  Numéro Twilio (Elastic SIP Trunk)            │      WhatsApp/Messenger
  ☎ PSTN ◀── appel sortant ──│                                               │◀──── API (WABA/Meta)
                              └──────────────────┬────────────────────────────┘           │
                                                  │ SIP (origination/termination)          │ POST
                                                  ▼                                        │ form-urlencoded
                              ┌─────────── LIVEKIT (Cloud ou self-hosted) ────────┐        │
                              │  SIP Trunk (inbound + outbound) + Dispatch Rule   │        │
                              │  → crée une Room, dispatch l'agent "jarvis"       │        │
                              │  → participant SIP porte le numéro appelant       │        │
                              │    (attribut sip.phoneNumber)                     │        │
                              └──────────────────┬────────────────────────────────┘        │
                                                  │ WebRTC (déjà existant, inchangé)         │
                                                  ▼                                        ▼
┌────────────────────────── SERVEUR JARVIS (FastAPI, HTTPS public) ─────────────────────────────┐
│                                                                                                │
│  interfaces/voice/agent.py (EXISTANT, INCHANGÉ)      interfaces/api/twilio.py (NOUVEAU, léger) │
│  ├── pipeline STT streaming (Deepgram) déjà en place  ├── POST /api/twilio/messaging/webhook   │
│  ├── turn-detection, TTS chunké déjà en place         │     validation signature (robuste)     │
│  ├── tool calling déjà branché sur tool_registry      │     → TwilioMessagingChannel           │
│  └── lit sip.phoneNumber → session_id (SessionKeyStore)                                        │
│                                                       interfaces/channels/                      │
│  capabilities/tools/phone_call.py (NOUVEAU, léger)    └── twilio_messaging.py                   │
│  └── LiveKit CreateSIPParticipant (pas twilio.rest)       gère fenêtre 24h + templates          │
│                                                                                                 │
│  ═══ CERVEAU (existant, inchangé) ═══                kernel/session_key_store.py (NOUVEAU)      │
│  engine/gateway.py : Gateway.handle(...)             SQLite WAL, remplace le JSON non verrouillé│
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Ce qui disparaît par rapport à la v1** : `interfaces/voice/twilio/media_stream.py`,
`stt_stream.py`, `audio.py` (transcodage μ-law) — plus nécessaires. Twilio ne parle plus
directement à notre serveur pour la voix ; il parle en SIP à LiveKit, qui parle en WebRTC à notre
agent existant. On économise ~300 lignes de code neuf et on hérite d'un pipeline déjà
latency-tuné plutôt que d'en écrire un nouveau, bloquant, à côté.

**Ce qui reste identique à la v1** : le principe directeur (rien ne contourne `Gateway.handle`),
le canal écrit WhatsApp/Messenger (webhook + `MessagingGateway` existant), et l'unification de
contexte par clé E.164.

### Flux voix entrant (v2)

```
Appel entrant → Twilio Elastic SIP Trunk → LiveKit Inbound Trunk
  → Dispatch Rule LiveKit : crée une Room + dispatch agent "jarvis" (pattern déjà utilisé
     par api/chat.py::get_voice_token pour les sessions navigateur)
  → agent.py lit participant.attributes["sip.phoneNumber"] → résout session_id via
     SessionKeyStore (normalisation twilio:+E164)
  → pipeline LiveKit existant : STT streaming, LLM, TTS chunké, barge-in — AUCUN CODE NEUF
  → fin d'appel → hooks existants (consolidation, auto_dream) s'exécutent normalement
```

### Flux voix sortant (v2)

```
PhoneCallTool.execute(numero, intention)
  → valider numero (regex E.164 stricte) avant tout usage
  → aiolimiter (1 req/s, cf. §3.4) → LiveKit CreateSIPParticipant(trunk_id=outbound_trunk,
     call_to=numero, room=nouvelle_room)
  → LiveKit compose l'appel via le SIP Trunk sortant → Twilio → PSTN
  → l'agent "jarvis" est dispatché dans la room comme pour un appel entrant ;
     `intention` est injectée comme premier tour de conversation (participant metadata)
```

### Flux écrit (WhatsApp / Messenger) — inchangé dans son principe, durci dans le détail

```
Message → Twilio → POST /api/twilio/messaging/webhook (form-urlencoded)
  → validation X-Twilio-Signature (reconstruction d'URL robuste, cf. §3.3)
  → IncomingMessage(platform, user_id=From, text=Body)
  → MessagingGateway.dispatch() (existant) → Gateway.handle → réponse
  → TwilioMessagingChannel.send() : texte libre SI dans la fenêtre 24h,
     sinon template Content API (cf. §3.4)
```

---

## 2. Configuration

### 2.1 Prérequis compte (inchangé)

| Étape | Détail |
|---|---|
| Compte + numéro | Numéro Voice+SMS (E.164). `ACCOUNT_SID`/`AUTH_TOKEN` (Console → Account Info). |
| URL publique HTTPS | Dev : `ngrok http 8000` **avec domaine réservé** (le plan v1 ignorait que l'URL éphémère du tier gratuit change à chaque restart et casse la config webhook — utiliser `ngrok http 8000 --domain=<réservé>` ou Tailscale Funnel). Prod : reverse proxy TLS permanent. |

### 2.2 Voix — Twilio Elastic SIP Trunking → LiveKit (remplace intégralement les Media Streams v1)

1. **Côté LiveKit** (Cloud ou self-hosted, déjà utilisé par le projet) :
   - Créer un **Inbound Trunk** LiveKit (accepte les appels entrants, associé au(x) numéro(s) Twilio).
   - Créer une **Dispatch Rule** qui, à réception d'un appel SIP entrant, crée une room et
     dispatch l'agent `jarvis` — même mécanisme que `agent_dispatch.create_dispatch(...)` déjà
     utilisé dans `interfaces/api/chat.py::get_voice_token`.
   - Créer un **Outbound Trunk** LiveKit pour les appels sortants (`PhoneCallTool`), pointant vers
     le trunk de terminaison Twilio.
   - Référence officielle à suivre pas à pas pour les noms exacts de ressources/API du SDK
     `livekit-api` installé : [docs.livekit.io/telephony/start/providers/twilio](https://docs.livekit.io/telephony/start/providers/twilio/).
2. **Côté Twilio** : Console → Elastic SIP Trunking → créer un trunk, Origination URI = URI SIP
   LiveKit (pour l'entrant), Termination = trunk sortant LiveKit (pour le sortant). Associer le
   numéro Twilio à ce trunk (Voice Configuration du numéro → pointer vers le trunk, plus de
   webhook `A call comes in` à configurer : le SIP remplace le webhook TwiML).
3. **Aucun serveur WebSocket Twilio à écrire** : `/ws/twilio/media` n'existe plus en v2.

### 2.3 WhatsApp (inchangé)

1. **Dev — Sandbox** : Console → Messaging → Try it out → WhatsApp sandbox (`join <code>`).
   - *When a message comes in* → `https://HOST/api/twilio/messaging/webhook` (POST)
2. **Prod** : WhatsApp Sender lié à un WABA Meta (vérification d'entreprise). Même webhook.
3. Format d'adresse : `whatsapp:+15145551234`.

### 2.4 Facebook Messenger

1. Console → Messaging → Channels → Facebook Messenger → connecter la Page (OAuth Meta).
2. Même webhook messaging.
3. **Avertissement conservé de l'audit** : ce channel est en *public beta* chez Twilio, sans SLA —
   traité comme canal best-effort, jamais comme dépendance critique.

---

## 3. Structure serveur & code

### 3.1 Arborescence (v2 — nettement plus légère que la v1)

```
src/jarvis/
├── interfaces/
│   ├── api/
│   │   └── twilio.py                  # NOUVEAU — webhook messaging uniquement (plus de TwiML voix)
│   ├── channels/
│   │   ├── twilio_messaging.py        # NOUVEAU — TwilioMessagingChannel + gestion fenêtre 24h
│   │   ├── base.py                    # existant — inchangé
│   │   ├── gateway.py                 # existant — inchangé
│   │   └── setup.py                   # MODIFIÉ — branche TWILIO_ENABLED
│   └── voice/
│       └── agent.py                   # EXISTANT — lit sip.phoneNumber, sinon inchangé
├── capabilities/tools/
│   └── phone_call.py                  # NOUVEAU — LiveKit CreateSIPParticipant (pas twilio.rest)
└── kernel/
    ├── settings.py                    # MODIFIÉ — champs twilio_* + livekit_sip_*
    └── session_key_store.py           # NOUVEAU — SQLite WAL, remplace messaging_sessions.json
```

**Suppressions actées par rapport à la v1** : `interfaces/voice/twilio/media_stream.py`,
`stt_stream.py`, `audio.py`. Dépendance `twilio` (SDK REST) conservée uniquement pour la
messagerie ; aucune dépendance audio/transcodage supplémentaire nécessaire côté Jarvis pour la voix.

### 3.2 Settings (`kernel/settings.py`)

```python
# ── Twilio (messagerie) ────────────────────────────────────
twilio_enabled: bool = Field(default=False)
twilio_account_sid: str = Field(default="")
twilio_auth_token: SecretStr = Field(default=SecretStr(""))
twilio_whatsapp_number: str = Field(default="", description="Sender WhatsApp, ex whatsapp:+14155238886")
twilio_messenger_page_id: str = Field(default="", description="messenger:<PAGE_ID>")
twilio_whatsapp_template_content_sid: str = Field(
    default="", description="ContentSid utilisé hors fenêtre 24h (cf §3.4)."
)
public_base_url: str = Field(default="", description="URL HTTPS publique pour la validation de signature.")
twilio_validate_signature: bool = Field(default=True)

@field_validator("twilio_validate_signature")
@classmethod
def _forbid_disabled_signature_in_prod(cls, v: bool, info) -> bool:
    # Assertion dure : on ne sort JAMAIS un webhook non authentifié en prod (cf. audit §3.1).
    if not v and info.data.get("environment") == "production":
        raise ValueError("twilio_validate_signature ne peut pas être désactivé en production.")
    return v

# ── LiveKit SIP (voix) ─────────────────────────────────────
livekit_sip_inbound_trunk_id: str = Field(default="")
livekit_sip_outbound_trunk_id: str = Field(default="")
max_concurrent_calls: int = Field(default=10, description="Sémaphore global voix, cf. audit §2.3/§5.")
```

### 3.3 Webhook messaging durci (`interfaces/api/twilio.py`)

```python
from fastapi import APIRouter, Request, Response
from twilio.request_validator import RequestValidator

router = APIRouter()


def _reconstruct_public_url(request: Request) -> str:
    """Reconstruit l'URL telle que Twilio l'a signée, en tenant compte d'un reverse
    proxy qui termine le TLS (cf. audit §3.1 : cause n°1 des échecs de validation
    en conditions réelles). settings.public_base_url reste la source de vérité pour
    le host, mais on ne s'appuie plus sur une simple concaténation statique."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    path = request.url.path.rstrip("/")  # normalise le trailing slash
    return f"{proto}://{host}{path}"


async def _validate(request: Request, form: dict) -> bool:
    if not settings.twilio_validate_signature:
        return True
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    signature = request.headers.get("X-Twilio-Signature", "")
    url = _reconstruct_public_url(request)
    return validator.validate(url, form, signature)


@router.post("/api/twilio/messaging/webhook")
async def messaging_webhook(request: Request) -> Response:
    form = dict(await request.form())
    if not await _validate(request, form):
        return Response(status_code=403)

    channel = request.app.state.twilio_messaging
    gateway = request.app.state.messaging_gateway
    msg = channel.parse_webhook(form)

    # Durabilité (audit §2.3) : on persiste AVANT l'ack, on ne fait plus un simple
    # fire-and-forget qui perd le message si dispatch() lève après le 200 OK.
    await request.app.state.pending_replies.enqueue(msg)
    return Response(content="<Response/>", media_type="application/xml")
```

Une tâche de fond (`pending_replies` — SQLite outbox, cf. §3.5) consomme la file et appelle
`gateway.dispatch(msg)`, avec retry en cas d'échec et marquage `done` seulement après un `send()`
réussi. Ainsi un crash process entre l'ack Twilio et la réponse effective ne perd plus le message.

### 3.4 Channel écrit avec gestion de la fenêtre 24h (`interfaces/channels/twilio_messaging.py`)

```python
from datetime import UTC, datetime, timedelta
from twilio.rest import Client
from jarvis.interfaces.channels.base import ChannelAdapter, IncomingMessage, MessageTarget, Platform

_SESSION_WINDOW = timedelta(hours=24)


class TwilioMessagingChannel(ChannelAdapter):
    platform = Platform.WHATSAPP

    def __init__(self, session_key_store) -> None:
        super().__init__()
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value())
        self._store = session_key_store   # SQLite — porte aussi last_inbound_at par identité

    def parse_webhook(self, form: dict) -> IncomingMessage:
        sender = form.get("From", "")
        platform = Platform.WHATSAPP if sender.startswith("whatsapp:") else Platform.MESSENGER
        self._store.mark_inbound(sender, datetime.now(UTC))   # horodatage pour la fenêtre 24h
        return IncomingMessage(platform=platform, user_id=sender, text=form.get("Body", ""), raw=form)

    async def send(self, reply: str, target: MessageTarget) -> None:
        import asyncio
        from_addr = (settings.twilio_whatsapp_number if target.user_id.startswith("whatsapp:")
                     else f"messenger:{settings.twilio_messenger_page_id}")

        last_inbound = self._store.last_inbound_at(target.user_id)
        outside_window = last_inbound is None or (datetime.now(UTC) - last_inbound) > _SESSION_WINDOW

        if outside_window and target.user_id.startswith("whatsapp:"):
            # Erreur 63016 sinon (cf. audit §1) : hors fenêtre, seul un template approuvé passe.
            if not settings.twilio_whatsapp_template_content_sid:
                logger.warning("Message hors fenêtre 24h et aucun ContentSid configuré — abandon.",
                               target=target.user_id)
                return
            await asyncio.to_thread(
                self._client.messages.create,
                from_=from_addr, to=target.user_id,
                content_sid=settings.twilio_whatsapp_template_content_sid,
                content_variables='{"1": "%s"}' % reply[:1000],
            )
            return

        await asyncio.to_thread(self._client.messages.create, from_=from_addr, to=target.user_id, body=reply)
```

### 3.5 Store de session unifié durable (`kernel/session_key_store.py`)

Remplace le `memory/messaging_sessions.json` non verrouillé (cf. audit §2.2) par SQLite en mode
WAL — écritures concurrentes gérées par le moteur, pas par un lock applicatif maison :

```python
import sqlite3

class SessionKeyStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_keys (
                identity_key TEXT PRIMARY KEY,   -- ex: "twilio:+15145551234"
                session_id TEXT NOT NULL,
                last_inbound_at TEXT
            )
        """)

    def resolve_or_create(self, identity_key: str, new_session_id_factory) -> str:
        row = self._conn.execute(
            "SELECT session_id FROM session_keys WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        if row:
            return row[0]
        sid = new_session_id_factory()
        self._conn.execute(
            "INSERT INTO session_keys (identity_key, session_id) VALUES (?, ?)", (identity_key, sid)
        )
        self._conn.commit()
        return sid

    def mark_inbound(self, identity_key: str, ts) -> None:
        self._conn.execute(
            "UPDATE session_keys SET last_inbound_at = ? WHERE identity_key = ?", (ts.isoformat(), identity_key)
        )
        self._conn.commit()

    def last_inbound_at(self, identity_key: str):
        row = self._conn.execute(
            "SELECT last_inbound_at FROM session_keys WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None
```

Partagé entre `MessagingGateway` (écrit) et `agent.py` (voix, via `sip.phoneNumber` normalisé en
`twilio:+E164`) — un seul store, un seul mécanisme de concurrence, plus de fichier JSON à
verrouiller manuellement.

### 3.6 Appels sortants (`capabilities/tools/phone_call.py`)

```python
import re
from aiolimiter import AsyncLimiter
from livekit import api as lk_api

_E164 = re.compile(r"^\+[1-9]\d{1,14}$")
_outbound_limiter = AsyncLimiter(1, 1)   # 1 appel/s — respecte le CPS Twilio par défaut (audit §1)


class PhoneCallTool(Tool):
    name = "phone_call"
    # tier confirm systématique — action externe niveau 5 CDC §10.1, comme LaunchAppTool

    async def execute(self, numero: str, intention: str, action: str = "run", **_: object) -> ToolResult:
        if not _E164.match(numero):
            return ToolResult(content=f"Numéro invalide (format E.164 attendu) : {numero}", is_error=True)
        if action != "confirm":
            self._pending[numero] = intention
            return ToolResult(content=f"Confirme l'appel vers {numero} ({intention}) avant lancement.")

        async with _outbound_limiter:
            lk = lk_api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
            room_name = f"jarvis-call-{numero.lstrip('+')}"
            await lk.sip.create_sip_participant(
                lk_api.CreateSIPParticipantRequest(
                    sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
                    sip_call_to=numero,
                    room_name=room_name,
                    participant_metadata=intention,   # récupéré par agent.py comme 1er tour
                )
            )
        return ToolResult(content=f"Appel initié vers {numero}.")
```

> Numéro validé par regex stricte **avant** tout usage (audit §3.2) — plus de f-string XML à
> risque d'injection : LiveKit expose une API structurée (`CreateSIPParticipantRequest`), pas de
> TwiML à construire à la main. Vérifier les noms exacts de méthodes/paramètres contre la version
> installée de `livekit-api` (référence : [docs.livekit.io/telephony](https://docs.livekit.io/telephony/start/providers/twilio/)).

---

## 4. Cycle de vie & contexte unifié chat ⇄ téléphone

Inchangé dans son principe par rapport à la v1 : clé normalisée `twilio:+E164` partagée entre
écrit et voix, mais portée maintenant par `SessionKeyStore` (SQLite, §3.5) au lieu du JSON.

| Canal | Adresse brute | Clé normalisée |
|---|---|---|
| Appel voix (via LiveKit SIP) | `participant.attributes["sip.phoneNumber"]` | `twilio:+15145551234` |
| WhatsApp | `whatsapp:+15145551234` | `twilio:+15145551234` |
| Messenger | `messenger:<PSID>` (opaque, pas de numéro) | `twilio:messenger:<PSID>` |

Cas Messenger : toujours pas de fusion automatique possible (pas de numéro) — sessions séparées
en v1/v2, table de liaison `psid → e164` restant une extension future optionnelle.

---

## 5. Sécurité & scalabilité

### Sécurité (corrections de l'audit intégrées)

1. **Signature webhook** : reconstruction d'URL robuste via `X-Forwarded-Proto`/`X-Forwarded-Host`
   (§3.3) — corrige le mode d'échec le plus commun en conditions réelles derrière un proxy.
   `twilio_validate_signature=False` est désormais **rejeté au boot** si `environment=production`
   (validator pydantic, §3.2) — impossible de le désactiver "temporairement" et d'oublier.
2. **Plus de WebSocket Twilio à sécuriser** : le SIP trunk supprime toute la surface d'attaque de
   `/ws/twilio/media` (pas d'auth WS à implémenter, pas de DoS économique par connexions WS non
   authentifiées — cf. audit §3.3). La sécurité du transport voix est portée par LiveKit
   (authentification SIP standard côté trunk) et par les tokens d'accès LiveKit déjà en place
   pour le reste du pipeline vocal.
3. **Injection TwiML** : éliminée pour la voix (plus de TwiML construit à la main côté sortant) ;
   regex E.164 stricte avant tout usage du numéro (§3.6).
4. **Durabilité messaging** : outbox SQLite (§3.3/§3.5) — un crash process n'efface plus un message
   déjà acquitté à Twilio.
5. **Rate limiting webhook** : le `pending_replies.enqueue()` (§3.3) découple l'ack Twilio du
   traitement, ce qui permet d'ajouter une limite de débit de consommation de la file (ex.
   `asyncio.Semaphore` sur le worker de dispatch) sans risquer un timeout Twilio — le flood ne se
   traduit plus directement en appels LLM non bornés.
6. **Secrets** : `TWILIO_AUTH_TOKEN` en `SecretStr`, jamais loggué ; redaction du `Body` en logs
   INFO (inchangé de la v1, toujours valide).
7. **Anti-abus voix** : whitelist de numéros appelants gérée côté **Dispatch Rule LiveKit**
   (rejet avant même que l'agent soit dispatché) plutôt que dans le handler applicatif — plus tôt
   dans la chaîne, donc moins coûteux en cas d'abus.

### Scalabilité

1. **Concurrence voix** : héritée du pipeline LiveKit existant, déjà dimensionné pour le cas
   navigateur — pas de sémaphore maison à réinventer pour la voix Twilio, `max_concurrent_calls`
   sert de garde-fou optionnel côté `PhoneCallTool` pour les sortants.
2. **CPS 1 appel/s** : respecté via `aiolimiter.AsyncLimiter(1, 1)` autour de la création de
   participant SIP sortant (§3.6) — documenté et géré plutôt qu'ignoré (audit §1).
3. **Multi-instance** : le `SessionKeyStore` SQLite/WAL supporte déjà des lecteurs/écrivains
   concurrents sur un même fichier ; migration vers un serveur SQLite partagé ou Postgres reste
   l'étape naturelle si le déploiement devient multi-process/multi-machine.
4. **Messaging** : timeout 15s Twilio respecté par le pattern ack-puis-file (§3.3), indépendamment
   de la charge du worker de dispatch.

---

## 6. Plan d'action étape par étape (v2)

| # | Étape | Livrable | Validation |
|---|---|---|---|
| 0 | Compte Twilio, `ngrok` avec domaine réservé, `uv add twilio aiolimiter`, settings | config | `client.api.accounts.get()` OK |
| 1 | **`SessionKeyStore` SQLite** : remplace le JSON existant de `MessagingGateway`, migration des données actuelles | fondation partagée | tests concurrence (2 écritures simultanées ne se perdent pas) |
| 2 | **Écrit WhatsApp (sandbox)** + validation signature robuste (§3.3) + assertion prod (§3.2) | messages bidirectionnels sécurisés | requête forgée sans signature → 403 ; requête légitime derrière proxy → 200 |
| 3 | **Outbox durable** (`pending_replies`) : ack immédiat, dispatch en file, retry | pas de message perdu sur crash | tuer le process pendant un dispatch en cours → message traité au redémarrage |
| 4 | **Gestion fenêtre 24h** (§3.4) + template `ContentSid` de test | conformité WhatsApp | message envoyé après 25h de silence simulé → template utilisé, pas d'erreur 63016 |
| 5 | **Messenger** : connexion page FB, test PSID (canal best-effort assumé) | 2ᵉ canal écrit | message page FB → réponse |
| 6 | **LiveKit SIP Trunk (inbound)** + Dispatch Rule → agent existant | appel entrant conversationnel via pipeline déjà optimisé | appeler le numéro, dialoguer, mesurer la latence réelle (cible réaliste ~1s, pas ~2s) |
| 7 | **`agent.py`** : lecture `sip.phoneNumber` → `SessionKeyStore.resolve_or_create` | continuité chat⇄tél | écrire sur WhatsApp puis appeler → même session, mémoire déjà chargée |
| 8 | **LiveKit SIP Trunk (outbound)** + `PhoneCallTool` (regex E.164, aiolimiter, tier confirm) | Jarvis appelle | "Jarvis, appelle X et dis-lui…" → appel + annonce |
| 9 | **Prod** : sender WhatsApp officiel, whitelist appelants au niveau Dispatch Rule, monitoring des codes d'erreur Media/SIP | mise en service | suite pytest + appel de bout en bout + injection de panne (kill Deepgram/Anthropic) vérifiée non silencieuse |

**Ordre volontaire, revu** : le store partagé et la durabilité du messaging passent en tout
premier (fondations transverses) avant même le premier canal — corrige l'ordre de la v1 qui
bâtissait le canal écrit sur un mapping JSON fragile dès l'étape 1.

---

## 7. Risques résiduels assumés (après corrections)

| Risque | Statut |
|---|---|
| Latence tool-calling en cours d'appel (ex. calendar, memory search) | Hérité du pipeline LiveKit existant — même compromis qu'en usage navigateur, pas spécifique à Twilio. Non aggravé, non éliminé. |
| Messenger sans identité téléphonique | Inchangé : sessions séparées en v1/v2, table de liaison en v3 éventuelle. |
| Messenger *public beta* Twilio | Assumé comme canal best-effort, jamais critique. |
| Panne LiveKit Cloud (si utilisé plutôt que self-hosted) | Nouveau SPOF introduit par le choix SIP — à mettre en balance avec le SPOF qu'il remplace (pont Media Streams maison, moins robuste et moins testé en prod que l'infra LiveKit). |
| Fallback STT local si Deepgram down | Porté par la config existante du pipeline LiveKit (déjà un sujet traité indépendamment de Twilio) — plus un risque spécifique à cette intégration. |
| Coûts (Twilio $/min + trunk LiveKit + Deepgram + LLM) | BudgetGuard existant : ajouter un tracker d'usage voix agrégé, indépendamment du transport (Media Streams ou SIP ne change pas ce besoin). |
