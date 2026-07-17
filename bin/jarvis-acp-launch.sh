#!/usr/bin/env bash
# bin/jarvis-acp-launch.sh
set -euo pipefail
export JARVIS_API_URL="${JARVIS_API_URL:-http://127.0.0.1:8000}"
export JARVIS_API_TOKEN="${JARVIS_API_TOKEN:?JARVIS_API_TOKEN doit être défini dans l'environnement du Gateway}"
export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src:${PYTHONPATH:-}"
exec python3 -m jarvis.interfaces.openclaw.acp_harness
