# Design — Jeu de tests de bout en bout pour la conversation téléphonique Twilio

Date : 2026-07-04
Statut : proposé

## Contexte

La conversation téléphonique Jarvis (Twilio Elastic SIP Trunking → LiveKit SIP →
pipeline vocal LiveKit Agents) traverse plusieurs composants indépendants :

1. **Appel sortant** — [`PhoneCallTool`](../../../src/jarvis/capabilities/tools/phone_call.py) :
   validation E.164, flow run/confirm avec expiration 5 min, rate-limit (1 appel/s),
   création d'un participant SIP LiveKit vers le trunk Twilio sortant.
2. **Appel entrant** — [`entrypoint()`](../../../src/jarvis/interfaces/voice/agent.py) et
   `_resolve_sip_caller_context()` : détection d'une room `jarvis-sip-*`, lecture de
   l'attribut `sip.phoneNumber` du participant SIP, résolution d'une session existante
   partagée avec le canal messagerie (clé `twilio:{numero}`).
3. **Pipeline vocal** — `_build_voice_stt` / `_build_voice_llm` / `_build_voice_tts` :
   sélection de provider par variable d'environnement, avec replis en cascade.
4. **Pont outils** — `_make_livekit_tool` / `_build_voice_tools` : expose les Tools
   Jarvis (schema Claude) comme `RawFunctionTool` LiveKit.

Il n'existe aujourd'hui aucun test automatisé pour cette chaîne. Les trois scripts
`tests/twilio_call.py`, `tests/twilio_sms.py`, `tests/twilio_messenger.py` (non
trackés, ajoutés manuellement) sont des scripts ad hoc hors convention pytest :
numéros en dur, aucune assertion, appels réels à l'API Twilio à chaque exécution.

Objectif : un jeu de tests qui permette, quand la téléphonie casse, d'identifier
en quelques secondes QUEL composant est en cause — sans dépendre d'un vrai appel
Twilio pour la majorité des cas.

## Approche retenue

Deux volets complémentaires (confirmé avec l'utilisateur) :

- **Volet 1 — suite pytest mockée** : rapide, isolée, zéro appel réseau réel,
  tourne en CI. Couvre la logique de chaque composant.
- **Volet 2 — script de diagnostic live** : sur le modèle existant de
  `scripts/validation/smoke_runtime.py` (scénarios en cascade, `--real` explicite,
  exit code + message PASS/FAIL par étape). Confirme qu'un vrai appel Twilio
  aboutit réellement ; remplace les 3 scripts ad hoc de `tests/`.

Alternative écartée : un seul gros test d'intégration bout-en-bout mockant
Twilio+LiveKit ensemble. Rejeté parce qu'un échec ne dit pas quel composant a
cassé (c'est précisément le problème que ce jeu de tests doit résoudre), et parce
que les mocks nécessaires (LiveKit RemoteParticipant, AgentSession, room events)
sont lourds à maintenir dans un seul test fourre-tout.

## Volet 1 — Suite pytest

### `tests/test_phone_call_tool.py`

Mock `lk_api.LiveKitAPI` (context manager async) et `settings.livekit_sip_outbound_trunk_id`.

- `test_numero_invalide_rejete` — numéro non E.164 → `ToolResult(is_error=True)`, aucun
  état `_pending` créé.
- `test_run_met_en_attente_sans_appeler_livekit` — action="run" ne touche jamais
  `lk_api.LiveKitAPI`.
- `test_confirm_sans_pending_echoue` — confirm sur un numéro jamais mis en attente
  → erreur explicite.
- `test_confirm_apres_expiration_echoue` — pending avec `expires_at` dans le passé
  (patch `datetime.now`) → erreur "délai expiré", entrée purgée de `_pending`.
- `test_confirm_sans_trunk_configure_echoue` — `settings.livekit_sip_outbound_trunk_id`
  vide → erreur explicite, `LiveKitAPI` jamais appelé.
- `test_confirm_cree_le_bon_participant_sip` — confirm valide → `create_sip_participant`
  appelé avec `sip_trunk_id`, `sip_call_to`, `room_name=f"jarvis-call-{numero_sans_plus}"`,
  `participant_metadata=intention`.
- `test_echec_livekit_renvoie_erreur_sans_lever` — `create_sip_participant` lève →
  `ToolResult(is_error=True)`, message contient le numéro.
- `test_rate_limiter_est_utilise` — patch du module-level `_outbound_limiter` par un
  spy (wrapper autour de `AsyncLimiter.acquire`) → un confirm déclenche bien une
  acquisition du limiter avant l'appel LiveKit. Pas de vrai sleep d'1s dans le test
  (on vérifie l'usage du limiter, pas son minutage réel).

### `tests/test_voice_sip_context.py`

Teste `_resolve_sip_caller_context` avec un `ctx` factice (`room.name`,
`room.remote_participants`).

- `test_room_non_sip_retourne_vide` — `room.name` ne commence pas par `jarvis-sip-`
  → `""`, aucune lecture de participants.
- `test_room_sip_avec_numero_nouveau_contact` — participant avec attribut
  `sip.phoneNumber`, `SessionKeyStore` vide → message "nouveau contact".
- `test_room_sip_avec_session_existante` — même numéro déjà présent dans
  `SessionKeyStore` (clé `twilio:{numero}`) → message mentionnant une conversation
  existante.
- `test_room_sip_sans_participant_apres_timeout` — `remote_participants` toujours
  vide → `""` après la boucle d'attente bornée (patch `asyncio.sleep` pour ne pas
  attendre 2s réelles), warning loggé.
- `test_session_key_store_inaccessible_ne_leve_pas` — `SessionKeyStore(...)` lève à
  la construction → fonction retourne quand même une chaîne cohérente ("nouveau
  contact"), pas d'exception propagée.

### `tests/test_voice_providers.py`

Teste `_build_voice_stt`, `_build_voice_tts`, `_build_voice_llm` en mockant les
classes de plugins LiveKit (`deepgram.STT`, `elevenlabs.TTS`, etc.) pour ne jamais
construire un vrai client réseau.

- STT : `STT_PROVIDER=openai|google|deepgram|<absent>` → bonne classe instanciée ;
  clé Deepgram absente/invalide/trop courte → log d'erreur explicite + repli
  Deepgram quand même (comportement actuel) ; provider inconnu lève à la
  construction → repli Deepgram.
- TTS : `TTS_PROVIDER=gemini` avec/sans clé ElevenLabs → `FallbackAdapter` vs
  Gemini seul (+ warning) ; `elevenlabs`/`openai`/`piper` (modèle présent/absent) ;
  aucune clé du tout → `ValueError` explicite.
- LLM : `API_BACKEND=openai|mistral|anthropic` → bon plugin + modèle par défaut
  correct ; `ImportError` sur le plugin → repli Gemini avec warning ; backend
  inconnu → Gemini direct.

### `tests/test_voice_tools_bridge.py`

- `test_make_livekit_tool_convertit_le_schema` — un `Tool` factice avec
  `to_claude_schema()` connu → `RawFunctionTool` avec `name`/`description`/
  `parameters` corrects.
- `test_execute_prefixe_erreur` — `ToolResult(is_error=True)` du tool sous-jacent →
  la fonction wrapper retourne une chaîne préfixée `[ERREUR]`.
- `test_execute_passe_le_contenu_si_ok` — `ToolResult(is_error=False)` → contenu
  renvoyé tel quel.

Ces 4 fichiers utilisent uniquement des mocks/fakes, pas de fixtures partagées
complexes — pas besoin de `conftest.py` (aucun fichier de tests existant du
projet n'en a un).

## Volet 2 — Script de diagnostic live

`scripts/validation/telephony_real_e2e.py`, structuré comme
`scripts/validation/smoke_runtime.py` : fonctions `_scenario_*(...) -> str | None`,
exécutées en cascade, arrêt au premier échec avec message `FAIL <étape> : <detail>`.

Étapes :

1. **Config** — `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` et
   `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` présents dans `.env`/env.
2. **LiveKit joignable** — `lk_api.LiveKitAPI().list_rooms()` (ou équivalent léger)
   répond sans lever.
3. **Twilio joignable** — un `GET Accounts/{sid}.json` (auth basique) confirme que
   les credentials sont valides, sans déclencher d'appel.
4. **Appel réel** (uniquement si `--real` ET `--to +E164` fournis) — invoque
   `PhoneCallTool` (run puis confirm) vers le numéro donné, avec une intention de
   test fixe, et affiche le SID Twilio résultant.

`argparse` : `--real` (obligatoire pour l'étape 4, sinon les 3 premières étapes
suffisent à un diagnostic sans coût), `--to` (numéro E.164 cible, pas de valeur
en dur), `--intention` (optionnelle).

## Migration des scripts existants

Suppression de `tests/twilio_call.py`, `tests/twilio_sms.py`,
`tests/twilio_messenger.py` (root `tests/`, non conventionnels, jamais commités) :

- L'appel voix est couvert par l'étape 4 de `telephony_real_e2e.py`.
- SMS et Messenger restent hors du périmètre "conversation téléphonique" de cette
  demande : pas remplacés ici (à traiter séparément si besoin — ce sont des
  scripts de messagerie, pas de voix).

## Hors périmètre

- Test du vrai Dispatch Rule LiveKit (provisionné hors dépôt, console LiveKit).
- Test de bout en bout du pipeline audio réel (STT→LLM→TTS avec vrai micro/réseau
  téléphonique) — nécessiterait un environnement LiveKit + Twilio complet, hors
  portée d'une suite de tests automatisée.
- Remplacement de `tests/twilio_sms.py` / `twilio_messenger.py` (hors scope voix).
