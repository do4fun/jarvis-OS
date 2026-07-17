# Plan « Realtime maison » — voix full-duplex self-hosted, Jarvis cerveau

## D'abord, un choix d'architecture honnête

Il existe deux façons de « recréer le realtime », et une seule est compatible avec vos contraintes (Jarvis = cerveau, français, budget) :

**Voie A — Modèle audio-natif (vrai speech-to-speech)** : Moshi de Kyutai, le seul open-source sous 300 ms, ~200 ms, tournant sur un seul GPU. Mais Moshi est **son propre cerveau** (7B, raisonnement limité) et anglais uniquement en 2026. Il viole vos deux contraintes principales. Éliminé comme voie principale.

**Voie B — Cascade optimisée full-duplex (recommandée)** : STT streaming + LLM streaming + TTS streaming, chaque étage démarrant avant que le précédent finisse. C'est ce que fait **Unmute** (Kyutai, open-source) : le plancher d'une cascade est ~500–800 ms, soit 200–400 ms de plus que l'audio-natif — un écart imperceptible dans la plupart des conversations, et la majorité des déploiements production 2026 utilisent des pipelines précisément parce qu'on contrôle le LLM. Autrement dit : Jarvis reste le cerveau.

**Cible réaliste à annoncer** : 600–900 ms par tour en self-hosted mono-GPU, ~500 ms bien optimisé. Le 300 ms est réservé à l'audio-natif ; personne ne l'atteint en cascade avec un vrai cerveau derrière.

## La stack retenue : Unmute (Kyutai) — taillée pour vous

Trois raisons qui en font le choix évident pour votre cas :

1. **Français natif** : `kyutai/stt-1b-en_fr` comprend anglais et français avec 500 ms de délai et un VAD sémantique intégré — le seul STT streaming open-source bilingue FR de ce calibre.
2. **Ça tient sur votre RTX 3090** : LLM ~6,1 Go + STT ~2,5 Go + TTS ~5,3 Go de VRAM = ~14 Go sur vos 24 Go, avec un docker-compose fourni.
3. **LLM interchangeable** : le backend Unmute accepte n'importe quel serveur compatible OpenAI (vLLM par défaut, Ollama, ou serveur externe) — c'est la porte d'entrée de Jarvis.

Les armes anti-latence intégrées : le VAD sémantique prédit la fin de parole (pas un simple seuil de silence), et le « flush trick » fait retraiter l'audio en accéléré (~4× temps réel) pour réduire l'attente post-parole de 500 ms à ~125 ms. Le TTS Kyutai commence à générer l'audio avant d'avoir reçu tout le texte — conçu exactement pour ce cas.

---

## Plan brique par brique

### Brique 0 — Stack Unmute isolée, validée au navigateur

Déployer le docker-compose Unmute tel quel (frontend + backend + STT + vLLM + TTS) sur la machine GPU. Valider une conversation vocale au micro dans le navigateur avec le LLM par défaut.
**Critère** : conversation fluide FR, latence de tour mesurée < 1 s. **Rollback** : `docker compose down`, rien ne touche Jarvis.

### Brique 1 — Jarvis devient le LLM du pipeline

C'est la brique clé. Unmute parle à son LLM via l'API OpenAI-compatible (`KYUTAI_LLM_URL` pointable vers n'importe quel serveur). Donc :

1. Créer dans `interfaces/api/` un endpoint `POST /v1/chat/completions` (streaming SSE, format OpenAI) qui route vers `engine.Gateway.handle(stream=True)` — votre streaming existe déjà pour LiveKit.
2. **Route rapide voix** dans ce chemin : modèle rapide, prompt court, mémoire pré-chargée en début de session, boucle d'outils désactivée par défaut. Chaque token compte : le TTS démarre au premier fragment de phrase.
3. Pointer `KYUTAI_LLM_URL` vers Jarvis (`host.docker.internal:8000`).

**Critère** : le pipeline vocal répond avec la personnalité et la mémoire de Jarvis ; premier audio audible < 1 s après fin de parole.

### Brique 2 — Le pont téléphonique

Unmute parle WebSocket navigateur ; il faut un pont vers Twilio. Deux options :

**Option 2a (moins de code) : réutiliser votre trunk SIP LiveKit.** Vous avez déjà LiveKit + SIP dans Jarvis. Écrire un agent LiveKit qui, au lieu du pipeline voix actuel, relaie l'audio vers le backend Unmute (WS bidirectionnel). LiveKit gère la téléphonie, les codecs et le jitter ; vous ne codez que le relais.

**Option 2b : pont Twilio Media Streams direct.** Un petit service Python : WS serveur pour Twilio (audio mulaw 8 kHz base64, messages `media`/`mark`/`clear`), transcodage mulaw 8 kHz ↔ PCM 24 kHz (audioop/ffmpeg), relais vers le WS Unmute. Valider **X-Twilio-Signature** sur la connexion — obligatoire selon Twilio.

Commencez par 2a : c'est votre infrastructure existante et le chemin le plus court.

### Brique 3 — Full-duplex : interruptions et fluidité

- **Barge-in** : quand le VAD sémantique détecte que l'utilisateur parle pendant que le bot parle → couper le flux TTS et (côté Twilio) envoyer le message `clear` pour vider le buffer audio.
- Activer le flush trick (déjà dans Unmute) et régler les seuils VAD.
- Instrumenter chaque étage : timestamp fin-de-parole → transcript final → premier token LLM → premier chunk audio. Sans ces quatre mesures, vous optimiserez à l'aveugle.

### Brique 4 — Le pattern « consult » pour les tâches lourdes

Même problème que le realtime commercial : un tour avec outils (recherche web, mission) prend 3–10 s. Solution identique à `openclaw_agent_consult`, et validée par la recherche Kyutai (MoshiRAG : récupération de connaissances **asynchrone** pendant que le modèle continue de parler) :

- La route rapide répond immédiatement (« je regarde ça ») et lance la tâche lourde en tâche de fond via votre `BackgroundWorker` existant.
- Le résultat est réinjecté dans la session au tour suivant, ou énoncé dès qu'il arrive.

### Brique 5 (option) — Compatibilité API Realtime

Le repo `huggingface/speech-to-speech` expose un serveur **compatible OpenAI Realtime** (`ws://…/v1/realtime`) au-dessus d'une cascade locale (Parakeet STT + LLM OpenAI-compatible + Qwen3-TTS). Intéressant en plan B ou pour rendre votre service consommable par tout client Realtime standard — y compris, ironiquement, le plugin voice-call d'OpenClaw.

---

## Budget latence cible (à vérifier brique par brique)

| Étage | Cible |
|---|---|
| Fin de parole → transcript final (VAD sémantique + flush trick) | ~125–250 ms |
| Premier token Jarvis (route rapide, streaming) | ~200–400 ms |
| Premier audio TTS (streaming, démarre avant la fin du texte) | ~150–300 ms |
| Réseau/transcodage téléphonie | ~50–150 ms |
| **Total premier audio** | **~550–1 000 ms** |

Pour descendre vers 450 ms : séparer STT/TTS/LLM sur des GPU distincts (Kyutai mesure ~750 ms mono-GPU → ~450 ms multi-GPU) — envisageable plus tard avec du cloud burst, cohérent avec votre stratégie hybride 3090 + cloud.

## Risques à surveiller

- **Contention VRAM** : LLM + STT + TTS + le reste de Jarvis sur 24 Go — dimensionnez le modèle vLLM en conséquence (7-8B quantisé max) ou déportez le LLM.
- **Voix TTS françaises** : le catalogue Kyutai est plus riche en anglais ; testez les voix FR du voice repository tôt, c'est un critère d'acceptation, pas un détail. Pocket TTS parle désormais français et tourne sur CPU — un fallback léger intéressant.
- **Écho téléphonique** : sans annulation d'écho, le bot s'entend lui-même via la ligne et se coupe ; le VAD sémantique aide mais testez ce cas explicitement en B2.
- **Linux requis** : les services Rust Kyutai visent Linux/WSL — encore un argument pour héberger ça hors de votre poste Windows.

**Ordre : B0 → B1 → B2a → B3 → B4**, chaque brique réversible, mesures de latence dès B0. Le différenciateur de tout ce plan est la B1 (route rapide streaming dans Jarvis) : c'est elle qui décide si vous êtes à 700 ms ou à 2 s.
