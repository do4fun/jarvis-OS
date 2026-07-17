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
