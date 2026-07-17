# OpenClaw Gateway — configuration voice-call realtime

Ce document décrit la procédure opérateur pour mettre en service le plugin
`voice-call` du Gateway OpenClaw (externe, Node.js, hors de ce repo) branché
sur Jarvis via le harnais ACP (`src/jarvis/interfaces/openclaw/acp_harness.py`,
Task 5) et l'endpoint `/api/openclaw/message` (route rapide voix, Task 4).

La configuration versionnée du Gateway vit dans `config/openclaw.json`
(JSON5, sans secrets — voir Global Constraints "SecretRefs"). Le lanceur du
harnais ACP est `bin/jarvis-acp-launch.sh`.

> **Statut** : cette procédure a été rédigée à partir de la documentation
> OpenClaw (recherche), **pas** en exécutant un Gateway réel. Les points
> marqués ⚠️ **À VÉRIFIER À L'EXÉCUTION** doivent être confirmés contre une
> instance Gateway vivante avant d'être traités comme définitifs. Ne pas
> copier-coller ces commandes en aveugle en production.

## 1. Lancement du Gateway en WSL2

Le Gateway OpenClaw tourne en Node.js sous WSL2 (pas nativement sous Windows).
Depuis un shell WSL2, à la racine du repo (montée via `/mnt/c/dev/jarvis-OS`
ou équivalent) :

```bash
export OPENCLAW_CONFIG_PATH="$(pwd)/config/openclaw.json"
openclaw gateway
```

`OPENCLAW_CONFIG_PATH` doit pointer vers `config/openclaw.json` créé
ci-dessus. Le Gateway charge cette config au démarrage : plugin `acpx` (agent
`jarvis-acp`, commande `bin/jarvis-acp-launch.sh`) et plugin `voice-call`
(Twilio + realtime Google).

⚠️ **À VÉRIFIER À L'EXÉCUTION** : nom exact du binaire/sous-commande
(`openclaw gateway` vs `openclaw gateway start`, etc.) et toute variable
d'environnement additionnelle requise par l'installation locale — voir
`openclaw --help` ou `docs.openclaw.ai/gateway/configuration`.

## 2. Génération du token Gateway

Le Gateway expose une API/WS protégée par un token que Jarvis (côté Python,
`OpenClawClient`, Task 1) doit présenter pour s'y connecter. Ce token est
distinct de `JARVIS_API_TOKEN` (qui protège l'API Jarvis dans l'autre sens,
utilisé par `bin/jarvis-acp-launch.sh`).

Étapes attendues :
1. Générer/obtenir le token via l'outil CLI du Gateway.
2. Le stocker dans `.env` (non versionné) sous la clé `OPENCLAW_GATEWAY_TOKEN`.

⚠️ **À VÉRIFIER À L'EXÉCUTION** : la commande exacte de génération du token
n'est pas encore relevée. Se référer à `openclaw --help` (sous-commande
probable du type `openclaw token create` ou `openclaw auth token`) ou à
`docs.openclaw.ai/gateway/configuration` au moment de l'exécution, puis
documenter ici la commande réelle utilisée.

## 3. Déclaration des SecretRefs

`config/openclaw.json` référence deux secrets par `secretRef` (jamais en
clair dans le fichier versionné) :
- `twilio-auth-token` — utilisé par `plugins.entries."voice-call".config.twilio.authToken`.
- `gemini-api-key` — utilisé par `plugins.entries."voice-call".config.realtime.providers.google.apiKey`.

Ces secrets doivent être enregistrés dans le magasin de secrets du Gateway
(pas dans `.env`, pas dans le JSON5) via une commande du type
`openclaw secrets set <nom> <valeur>`.

⚠️ **À VÉRIFIER À L'EXÉCUTION** : la syntaxe exacte de `openclaw secrets set`
(arguments positionnels vs flags, lecture depuis stdin ou fichier, éventuel
besoin de préciser un scope/agent) doit être relevée dans
`docs.openclaw.ai/gateway/configuration` ou via `openclaw secrets --help`
contre une instance réelle avant d'être considérée comme définitive. Une fois
confirmée, remplacer ce paragraphe par la commande exacte utilisée pour
`twilio-auth-token` et `gemini-api-key`.

Les autres valeurs (`TWILIO_ACCOUNT_SID`, `OPENCLAW_VOICE_NUMBER`,
`PUBLIC_BASE_URL`) sont interpolées depuis l'environnement du Gateway
(`${VAR}`) — non-secrètes ou déjà couvertes par un secret séparé, elles
peuvent vivre dans `.env`.

## 4. Configuration webhook voix Twilio Console

Une fois le Gateway démarré avec le plugin `voice-call` actif, celui-ci
expose une URL de webhook voix (loggée au démarrage du Gateway — chercher une
ligne mentionnant le port/chemin du plugin `voice-call`).

Dans la Console Twilio, sur le numéro dédié (`OPENCLAW_VOICE_NUMBER`) :
1. Section "Voice & Fax" → "A call comes in".
2. Configurer le webhook avec l'URL publique exposée par le tunnel ngrok
   (voir section 5) qui pointe vers le port local loggé par le Gateway.
3. Méthode HTTP : celle attendue par le plugin (typiquement `HTTP POST`,
   à confirmer dans les logs/doc du plugin `voice-call`).

⚠️ **À VÉRIFIER À L'EXÉCUTION** : le chemin exact de l'URL de webhook (ex.
`/voice-call/incoming`) n'est connu qu'au démarrage réel du Gateway — le
relever dans les logs et le documenter ici.

## 5. Tunnel ngrok additionnel pour le webhook voix

`PUBLIC_BASE_URL` (utilisé par `plugins.entries."voice-call".config.publicUrl`)
correspond déjà à un tunnel ngrok existant pour l'API Jarvis. Le webhook voix
Twilio du plugin `voice-call` écoute sur un port **distinct** côté Gateway
(le port du plugin, pas celui de `gateway.port` = 18789 utilisé pour l'ACP),
et nécessite donc son propre tunnel ngrok :

```bash
ngrok http <port-du-plugin-voice-call>
```

L'URL publique ngrok obtenue est celle à renseigner dans la Console Twilio
(section 4).

⚠️ **À VÉRIFIER À L'EXÉCUTION** : le port exact écouté par le plugin
`voice-call` pour les webhooks entrants n'est visible qu'au démarrage réel du
Gateway (logs) — le relever et le documenter ici, ainsi que l'URL ngrok
définitive une fois stabilisée (idéalement un domaine ngrok réservé pour
éviter de reconfigurer la Console Twilio à chaque redémarrage).

## Référence croisée

- Client WS OpenClaw : `src/jarvis/interfaces/openclaw/` (Tasks 1-3).
- Endpoints `/api/openclaw/health` et `/message` : Task 4.
- Harnais ACP (`jarvis-acp`) : `src/jarvis/interfaces/openclaw/acp_harness.py` (Task 5).
- Wiring boot / shutdown symétrique : Task 6 (`setup_openclaw()` dans `app.py`).
- Outil `voice_call` (appels sortants) : Task 7.
- Plan complet : `docs/architecture/2026-07-16-implementation-telephonie-twilio-realtime.md`.
