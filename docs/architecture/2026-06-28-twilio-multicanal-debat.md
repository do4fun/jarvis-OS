# Audit critique — Plan Twilio/Jarvis multicanal

> Contre-analyse du plan initial ([2026-06-28-twilio-multicanal.md](./2026-06-28-twilio-multicanal.md)),
> menée en posture SRE/sécurité avec vérification des affirmations contre la documentation
> Twilio/Deepgram/LiveKit à jour. Sert de justification aux changements de
> [2026-06-28-twilio-multicanal-v2.md](./2026-06-28-twilio-multicanal-v2.md).

Verdict global : le plan initial est **structurellement correct** (webhooks/WS bien localisés,
principe "tout passe par `Gateway.handle`" sain) mais il a été écrit en supposant un monde sans
pannes, sans concurrence et sans limites d'API. Trois familles de problèmes cassent en prod :
**latence de bout en bout non maîtrisée**, **absence de verrouillage sur l'état partagé**, et
**mitigations de sécurité décrites en prose mais absentes du code**.

---

## 1. Limitations de l'API Twilio ignorées par le plan

**Format audio** — le plan initial est correct sur le fond (`audio/x-mulaw`, 8000 Hz, base64, sans
header de fichier), confirmé par la doc officielle
[Media Streams – WebSocket Messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages).
Ce qui manque :

| Ignoré | Impact |
|---|---|
| **CPS par défaut = 1 appel sortant/seconde** sur `calls.create()` ([source](https://support.twilio.com/hc/en-us/articles/223180028-How-Fast-Can-I-Place-or-Receive-Phone-Calls-with-Twilio)) — n'affecte PAS les appels entrants, seulement l'initiation via API | `PhoneCallTool` invoqué en rafale (retry sur erreur, ou plusieurs contacts) se fait **mettre en file d'attente silencieusement** par Twilio. Le plan initial ne le mentionne nulle part dans §5 Scalabilité. |
| **Codes d'erreur Media Streams** : 31901 (timeout connexion WS), 31902 (connexion refusée), 31921 (fermeture anormale), 31941/31942 (mauvaise config track) ([source](https://www.twilio.com/docs/api/errors/31921)) | Aucune gestion de ces cas — ni retry, ni log distinctif, ni alerte. Un appel qui meurt côté Twilio pour une de ces raisons est silencieux côté Jarvis. |
| **Débit de messages WS** : ~50 Hz bidirectionnel par appel (frame 20 ms), JSON + base64 (overhead ~33%) | §5 ne parle que du sémaphore LLM. Le coût CPU réel de `json.loads`/`base64.b64decode` à cette cadence, multiplié par N appels simultanés, n'est jamais chiffré ni testé. |
| **Facebook Messenger channel = *public beta*** chez Twilio ([source](https://www.twilio.com/docs/messaging/channels/facebook-messenger)) | Aucune SLA. Le plan traite Messenger comme un canal de production au même titre que WhatsApp — c'est un pari, pas un fait acquis. |
| **Fenêtre de 24h WhatsApp/Messenger** (erreur `63016` hors fenêtre, [source](https://www.twilio.com/docs/api/errors/63016)) | Après 24h sans message entrant de l'utilisateur, tout `messages.create(body=...)` en texte libre **échoue**. `TwilioMessagingChannel.send()` n'a **aucune branche template** — et le plan prétend brancher les notifications proactives existantes (`ProactiveQueue`) sur ce canal. Une alerte météo proactive envoyée à un utilisateur silencieux depuis 25h **échouera systématiquement**. |

---

## 2. Failles architecturales et SPOF

### 2.1 La boucle temps réel est sérialisée, pas parallélisée — c'est le vrai problème de fluidité

Le code initial (§3.5) fait : `Gateway.handle(texte, stream=False)` PUIS
`await tts_engine.synthesize(str(response))` sur le **texte complet**. Décomposons la latence
réelle d'un tour :

```
fin de parole utilisateur
  → 300ms  (endpointing Deepgram avant speech_final=true)
  → 800ms–3s+ (LLM Anthropic, PLUS si un tool est appelé — calendar, weather, memory search…)
  → temps de synthèse TTS COMPLET (ElevenLabs/Piper génèrent tout le texte avant le 1er octet)
  → transcodage (négligeable)
= 2 à 6+ secondes de silence total avant le premier son
```

Le §6 étape 4 du plan initial promet une validation "< ~2s/tour" — ce chiffre est **optimiste et
non vérifié**, et devient faux dès qu'un tool est invoqué. En téléphonie, l'attente humaine
tolérée est de l'ordre de 700ms–1s avant qu'un interlocuteur ne pense que la ligne a coupé. Le
plan initial livre une expérience "vive Alexa en 2016", pas une conversation fluide.

**Le plus grave** : ce problème est **déjà résolu** dans le codebase. Le pipeline vocal LiveKit
existant (`interfaces/voice/agent.py`) fait exactement du streaming STT→LLM→TTS token par token
avec turn-detection. Le plan initial réinvente à côté un pipeline naïf entièrement bloquant, au
lieu de réutiliser l'existant.

### 2.2 Race condition explicite sur le contexte partagé

Réponse directe à la question posée : *"comment la centralisation peut-elle casser si deux
événements arrivent en même temps ?"*

Le mapping `session_key → session_id` vit dans **un seul fichier JSON non verrouillé**
(`messaging_sessions.json`). Scénario concret :
- L'utilisateur envoie un message WhatsApp **au même instant** où il raccroche un appel.
- Les deux flux (`MessagingGateway.dispatch` et `media_stream.py` finally) font un
  **read-modify-write** sur ce fichier, sans lock, sans écriture atomique (`json.dump` direct,
  pas de tmp+`os.replace`).
- Résultat possible : la dernière écriture gagne et efface la mise à jour de l'autre (l'utilisateur
  "perd" sa session voix ou son historique WhatsApp se retrouve associé au mauvais `session_id`),
  ou pire, un écrasement partiel corrompt le JSON pour **tous les utilisateurs simultanément**
  (un seul fichier = un seul point de contention global).

Autre race non traitée : rien ne sérialise les tours dans `media_stream.py`. Si Deepgram émet
deux `speech_final=true` rapprochés (fréquent avec un `endpointing` court), `on_transcript` peut
s'exécuter deux fois en concurrence sur le **même** `session_id` → deux appels `gateway.handle()`
simultanés, historique de conversation entrelacé, coût LLM doublé, et le `speaking` `asyncio.Event`
partagé entre les deux exécutions produit un état incohérent (l'un clear l'audio de l'autre).

### 2.3 SPOF non adressés

| SPOF | Conséquence |
|---|---|
| Un seul process uvicorn héberge webhooks + WS + appels LLM | Un crash (exception non catchée dans une tâche WS, hot-reload `_watch_dotenv` existant, pic mémoire Whisper local) **tue tous les appels en cours instantanément**. |
| `asyncio.create_task(gateway.dispatch(msg))` fire-and-forget dans le webhook messaging | Si `dispatch()` lève une exception, **rien ne le rattrape** — et comme le `200 OK` a déjà été renvoyé à Twilio, **il ne réessaiera jamais**. Message perdu, silence radio pour l'utilisateur, personne n'est alerté. Aucune queue durable (Redis, SQLite outbox) ne protège ce chemin. |
| Deepgram down / clé invalide | `stt.connect()`/`stt.send()` lève, non catché dans la boucle WS → le handler d'appel entier plante. Le "fallback Whisper local" du §3.6 initial est **de la prose, pas du code** — aucun chemin d'exécution ne l'implémente réellement. |
| Anthropic API down | SPOF transversal à TOUS les canaux simultanément, aucun circuit breaker, aucune réponse de repli. |

---

## 3. Vulnérabilités de sécurité

### 3.1 Validation de signature : le vrai risque n'est pas son absence, c'est son échec silencieux

Le mode d'échec documenté le plus courant chez Twilio : un reverse proxy fait la terminaison TLS,
l'app voit `http://` mais Twilio a signé `https://` → mismatch → 403 sur des requêtes
**légitimes** ([source](https://www.twilio.com/en-us/blog/developers/tutorials/building-blocks/handle-ssl-termination-twilio-node-js-helper-library)).
Le code initial (§3.3) reconstruit l'URL via simple concaténation
`public_base_url + request.url.path`, sans lire `X-Forwarded-Proto`/`X-Forwarded-Host`, sans
gérer trailing slash ou port. **Le scénario réaliste** : le développeur déploie derrière
Caddy/nginx, la validation échoue systématiquement sur du trafic légitime, et — sous pression pour
que "ça marche" — finit par mettre `twilio_validate_signature=False`. Le webhook reste alors
**définitivement ouvert**, sans authentification, avec accès direct au `tool_registry` de Jarvis
(filesystem, gmail, calendar, launch_app…) via n'importe quel POST forgé avec un `From` arbitraire.

### 3.2 Injection TwiML par f-string (incohérence interne au plan)

`voice_incoming` (§3.3 initial) utilise correctement le builder `VoiceResponse`/`Connect`
(échappement automatique). Mais `PhoneCallTool` (§3.8 initial) construit le TwiML sortant par
**f-string brute** :

```python
twiml=f'...<Parameter name="caller" value="{numero_e164}"/>...'
```

`numero_e164` provient d'une commande vocale résolue par le LLM — rien ne garantit un format
E.164 strict avant interpolation. Un caractère `"` ou `<` casse le XML ou permet d'injecter un
élément/attribut arbitraire dans le TwiML qui pilote l'appel. C'est le chemin le plus faible du
plan initial, alors que le chemin voisin (même fichier) fait ça correctement.

### 3.3 WS non authentifié en pratique

Le §5 sécurité du plan initial *mentionne* un JWT éphémère en query string et une vérification
d'`accountSid` — mais le code exemple `media_stream.py` fait `await ws.accept()` **sans aucune
vérification**, littéralement en première ligne. Tant que ce n'est que de la prose non
implémentée : n'importe qui devinant/scannant l'URL `/ws/twilio/media` ouvre une connexion,
déclenche une session Deepgram payante et consomme un slot du sémaphore de concurrence — DoS
économique et fonctionnel sans qu'un seul vrai appel téléphonique n'existe.

### 3.4 Pas de limite de débit sur le webhook messaging

Rien ne plafonne le nombre de `asyncio.create_task(gateway.dispatch(...))` créées. Un flux de
POST (légitime en volume, ou via une signature mal validée comme en 3.1) peut lancer un nombre
non borné d'appels LLM concurrents, contournant l'intention du `BudgetGuard` déjà présent dans le
projet — rien en amont du dispatch ne throttle le volume entrant lui-même.

---

## 4. Recommandations concrètes → reportées dans la v2

| # | Problème | Correction | Où dans la v2 |
|---|---|---|---|
| **1 (majeur)** | Pipeline voix entièrement sérialisé et réinventé | **Remplacer Media Streams par LiveKit SIP + Twilio Elastic SIP Trunking.** Twilio route l'appel en SIP direct vers un endpoint LiveKit ([doc officielle](https://docs.livekit.io/telephony/start/providers/twilio/), [guide pratique](https://www.iqbsys.com/blog/connect-inbound-calls-to-a-voice-ai-agent-with-twilio-and-livekit)) : le pipeline `interfaces/voice/agent.py` **déjà existant, déjà optimisé** récupère l'appel sans une seule ligne de `media_stream.py`/`stt_stream.py`/`audio.py` à écrire. | §1, §3 (architecture voix remplacée) |
| **2** | Race condition sur `messaging_sessions.json` | SQLite en mode WAL (transactionnel gratuit) au lieu du JSON non verrouillé. | §4.1 |
| **3** | Tours concurrents sur un même appel | Non-applicable si SIP/LiveKit repris (le turn-detection LiveKit gère déjà ça) ; documenté comme risque résiduel si Media Streams est gardé en secours. | §7 (fallback) |
| **4** | Dispatch fire-and-forget sans durabilité | Table `pending_replies` (SQLite) écrite avant l'ack Twilio, marquée `done` après `send()` réussi, boucle de retry en arrière-plan. | §3.3, §5 |
| **5** | Fallback STT non implémenté | Non-applicable en v2 (LiveKit gère STT/fallback) ; documenté comme risque résiduel si Media Streams est gardé en secours. | §7 |
| **6** | Fenêtre 24h WhatsApp/Messenger | Tracker le timestamp du dernier message entrant par identité ; branche `ContentSid`/template si hors fenêtre. | §3.4 |
| **7** | Signature derrière proxy/tunnel | Reconstruction via `X-Forwarded-Proto`/`X-Forwarded-Host`, assertion au boot interdisant `twilio_validate_signature=False` en prod. | §3.3 |
| **8** | Injection TwiML dans `PhoneCallTool` | Regex E.164 stricte + builder `VoiceResponse` partout, jamais de f-string XML. | §3.8 |
| **9** | WS non authentifié | JWT courte durée vérifié avant `ws.accept()`, scrubbé des logs, allow-list IP Twilio au reverse-proxy. | §5 |
| **10** | DoS webhook/WS | Sémaphore acquis avant l'ouverture de connexion STT, throttle sur le webhook messaging. | §5 |
| **11** | CPS 1/s sur appels sortants | `aiolimiter.AsyncLimiter(1, 1)` autour de `calls.create()`. | §3.8 |

**Décision structurante retenue pour la v2** : le fix #1 (SIP + LiveKit) change la nature même du
plan — il ne s'agit plus de construire un pont Media Streams maison, mais de brancher Twilio en
frontal SIP sur l'infrastructure vocale déjà existante et déjà durcie. Ce choix résout de facto
les points #1, #3, #5 et une bonne partie de la latence (§2.1) sans code supplémentaire ; les
autres corrections (#2, #4, #6, #7, #8, #9, #10, #11) restent nécessaires indépendamment de ce
choix et sont reportées telles quelles dans la v2.
