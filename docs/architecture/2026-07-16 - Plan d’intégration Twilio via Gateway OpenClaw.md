# Stratégies pour la voix Twilio via OpenClaw avec Jarvis comme cerveau — classées par gain de latence

D’abord le cadre : votre budget total par tour est ~1,5 s pour que ça reste naturel (au-delà de 400 ms de RTT, la conversation prend un ressenti talkie-walkie). Chaque maillon doit donc être budgété : fin de parole détectée (~300–500 ms) + STT final (~100 ms) + cerveau (~500–700 ms jusqu’au premier token) + TTS TTFB (~100–200 ms) + réseau. Voici les trois architectures possibles, de la plus efficace à la plus simple.

-----

## Stratégie A (recommandée) : hybride realtime + « agent consult » — Jarvis reste le cerveau décisionnel

Votre plan a exclu le mode realtime au motif qu’il « répond avec son propre modèle ». C’est vrai pour les tours triviaux, mais le plugin a précisément un mécanisme pour votre cas : `talk.client.toolCall` permet aux transports realtime de forwarder les tool calls du provider vers la policy du gateway, et le premier tool supporté est `openclaw_agent_consult`  — c’est-à-dire déléguer au cerveau complet. Et `realtime.agentContext` injecte une capsule de contexte de l’agent une seule fois à la création de la session (zéro latence par tour), tandis que `openclaw_agent_consult` exécute l’agent complet pour le travail d’outils, les infos actuelles, les lookups mémoire ou l’état du workspace.

Concrètement pour vous :

- Le modèle realtime (Gemini Live / OpenAI Realtime, speech-to-speech natif, ~300–600 ms) gère la mécanique conversationnelle : accusés de réception, reformulations, barge-in.
- Dès qu’il y a une **décision, un outil, ou de la mémoire**, il appelle `openclaw_agent_consult` → binding ACP → votre harnais → Jarvis. Jarvis reste le cerveau de référence ; pendant la consultation, le modèle realtime meuble naturellement (« je vérifie ça »), ce qui masque les 2–4 s de votre boucle ACP.
- La capsule `agentContext` fait parler le modèle realtime avec la personnalité et le contexte de Jarvis.

C’est le seul schéma qui concilie vos deux contraintes (Jarvis = cerveau, latence humaine) sans réécrire votre pipeline. Le compromis honnête : les tours purement conversationnels ne passent pas par Jarvis — mais ils ne portent aucune décision, et tout ce qui compte y passe.

## Stratégie B : mode transcription optimisé (votre plan actuel, corrigé)

Si vous tenez au « chaque tour passe par Jarvis », il faut attaquer chaque maillon :

**1. Streaming de bout en bout — non négociable.** Votre ACP v1 « réponse en un bloc » est le tueur de latence. Le bridge ACP d’OpenClaw gère nativement le routage de session, la livraison de prompts et les updates en streaming  — utilisez `session/update` en chunks dès la v1 voix. Côté Jarvis : `Gateway.handle(stream=True)` existe déjà pour LiveKit, exposez un endpoint SSE et faites relayer les chunks phrase par phrase par le harnais. Le TTS démarre à la première phrase, pas à la fin de la génération.

**2. Route rapide voix dans `engine.Gateway`.** Sur `meta.channel="voice"` : modèle rapide (Haiku/équivalent), prompt système court, mémoire pré-chargée en début d’appel plutôt que recherchée à chaque tour, et boucle d’outils désactivée par défaut (ou limitée à 1 outil avec message d’attente vocal envoyé immédiatement). Notez qu’OpenClaw lui-même a suivi cette voie : la v2026.6.10 a introduit un « fast mode » automatique pour les tours conversationnels courts, qui revient en mode normal pour les runs plus longs  — mettez à jour et bénéficiez-en.

**3. Détection de fin de tour.** Le défaut `silenceDurationMs` est 800 ms  — c’est presque un tiers de votre budget perdu avant même de commencer. Descendez à 400–500 ms, et préférez **Deepgram Flux** comme provider STT : détection de tour sémantique (Flux), TTFB de 90 ms côté TTS Aura-2, et barge-in natif.  La détection sémantique (la phrase est-elle finie ?) bat le simple seuil de silence.

**4. TTS en streaming téléphonique.** ElevenLabs Flash ou Deepgram Aura-2 en sortie mulaw 8 kHz directe — le payload renvoyé à Twilio doit être audio/x-mulaw 8000 Hz base64,  donc tout TTS qui sort nativement ce format évite un transcodage. Envoyez phrase par phrase avec des messages `mark` pour synchroniser, et `clear` pour le barge-in.

**5. Réseau : supprimez les sauts.** Chaque tour de votre plan actuel traverse ngrok + WSL2 + `host.docker.internal`. Colocalisez Gateway et Jarvis sur la même machine Linux (VPS ou mini-PC), tunnel à hostname fixe (Cloudflare Tunnel) ou IP publique directe du VPS, et région Twilio la plus proche. Gagner 100–200 ms de réseau par tour est le levier le moins cher de toute la liste.

**6. Mesurez dès le premier appel.** L’outil `latency` intégré lit `calls.jsonl` et sort p50/p90/p99 des latences de tour et des temps d’attente d’écoute  — instrumentez avant d’optimiser, et fixez un SLO (p90 < 2 s en mode transcription est réaliste ; < 1,2 s ne l’est pas avec un tour complet Jarvis).

**Plafond honnête de la stratégie B** : même optimisée, un pipeline STT→LLM→TTS séquentiel tourne autour de 800 ms–1,5 s dans le meilleur des cas, contre ~600 ms pour un pipeline realtime. Avec le hop ACP+HTTP en plus, visez 1,5–2,5 s par tour — utilisable, pas « naturel ».

## Stratégie C : ne pas mettre la téléphonie derrière OpenClaw

Option structurelle à considérer froidement : Jarvis a déjà un pipeline voix temps réel LiveKit avec trunk SIP. LiveKit gère nativement le streaming, l’interruption et le VAD, et votre agent vocal y est déjà branché sur `Gateway.handle`. Brancher un second numéro Twilio sur le trunk SIP LiveKit existant vous donnerait la voix à latence maîtrisée **sans** le hop Gateway→ACP→HTTP, en réservant OpenClaw aux canaux texte (WebChat, SMS, WhatsApp, Telegram) où la latence est indolore. Vous perdez l’unification des canaux voix/texte dans OpenClaw, mais vous gagnez un maillon de moins dans la chaîne la plus sensible.

-----

## Ma recommandation d’arbitrage

Faites la **B2 en deux temps** : d’abord la stratégie A (realtime + `openclaw_agent_consult` + `agentContext`) comme mode nominal — c’est ce que le plugin est conçu pour faire, et Jarvis garde la main sur tout ce qui est décisionnel. En parallèle, construisez le streaming ACP + route rapide (stratégie B) comme socle, car il servira aussi les canaux texte et vous permettra plus tard de basculer en full-transcription si le compromis « le realtime gère les tours simples » vous gêne à l’usage. Gardez la stratégie C en tête comme sortie de secours si les mesures p90 restent au-dessus de 2,5 s après optimisation — vos données `calls.jsonl` trancheront, pas l’intuition.

Deux garde-fous transverses quel que soit le choix : testez le scénario du bug connu où le greeting démarre pendant que le provider de transcription se connecte encore  (appels « sourds »), et vérifiez que les credentials du plugin passent par des SecretRefs plutôt qu’en clair dans le JSON versionné.
