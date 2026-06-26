# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from loguru import logger

from jarvis.capabilities.tools.base import Tool, ToolResult

_APPROVAL_TTL = timedelta(minutes=5)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)  # Win32 only; 0 = no-op elsewhere


class _PendingLaunch:
    __slots__ = ("alias", "cmd", "description", "expires_at")

    def __init__(self, alias: str, cmd: list[str], description: str) -> None:
        self.alias = alias
        self.cmd = cmd
        self.description = description
        self.expires_at = datetime.now(UTC) + _APPROVAL_TTL


def _resolve_exe(exe_name: str, search_paths: list[str]) -> str | None:
    found = shutil.which(exe_name)
    if found:
        return found
    for dir_str in search_paths:
        candidate = Path(dir_str) / exe_name
        if candidate.exists():
            return str(candidate)
    return None


class LaunchAppTool(Tool):
    """Lance des applications Windows locales depuis le catalogue config/apps.yaml.

    tier safe    : Popen non-bloquant, retour immédiat.
    tier confirm : mise en attente TTL 5 min, relance avec action='confirm'.
    """

    name = "launch_app"

    def __init__(self, catalog_path: Path | None = None) -> None:
        self._apps: dict[str, dict] = {}
        self._pending: dict[str, _PendingLaunch] = {}

        if catalog_path is None:
            from jarvis.kernel.paths import CONFIG_DIR

            catalog_path = CONFIG_DIR / "apps.yaml"

        if catalog_path.exists():
            data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
            self._apps = data or {}

        safe_names = [k for k, v in self._apps.items() if v.get("tier", "safe") == "safe"]
        confirm_names = [k for k, v in self._apps.items() if v.get("tier") == "confirm"]
        aliases = ", ".join(self._apps) or "aucune (édite config/apps.yaml)"

        self.description = (
            f"Lance une application Windows installée localement. "
            f"Applications disponibles : {aliases}. "
            f"Sans confirmation : {safe_names}. "
            f"Avec confirmation vocale requise : {confirm_names}. "
            "Pour confirmer une app en attente, passe action='confirm'."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": (
                        f"Alias de l'application à lancer. "
                        f"Disponibles : {', '.join(self._apps) or 'aucune'}"
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["run", "confirm"],
                    "description": (
                        "'run' lance immédiatement (safe) ou met en attente (confirm). "
                        "'confirm' valide une app précédemment mise en attente."
                    ),
                },
            },
            "required": ["app"],
        }

    async def execute(self, app: str, action: str = "run", **_: object) -> ToolResult:
        if action == "confirm":
            return self._confirm_pending(app)

        entry = self._apps.get(app)
        if entry is None:
            available = ", ".join(self._apps) or "aucune"
            return ToolResult(
                content=f"Application inconnue : '{app}'. Disponibles : {available}",
                is_error=True,
            )

        exe_name = entry["command"][0]
        extra_args = entry["command"][1:]
        search_paths: list[str] = entry.get("paths", [])
        description = entry.get("description", exe_name)
        tier = str(entry.get("tier", "safe")).lower()

        resolved = _resolve_exe(exe_name, search_paths)
        if resolved is None:
            hint = f" (chemins cherchés : {search_paths})" if search_paths else ""
            return ToolResult(
                content=(
                    f"Exécutable '{exe_name}' introuvable{hint}. "
                    "Vérifiez l'installation ou ajoutez le chemin dans config/apps.yaml."
                ),
                is_error=True,
            )

        cmd = [resolved] + extra_args

        if tier == "confirm":
            self._pending[app] = _PendingLaunch(alias=app, cmd=cmd, description=description)
            logger.info("LaunchApp awaiting approval", alias=app)
            return ToolResult(
                content=(
                    f"'{description}' nécessite une confirmation avant lancement.\n"
                    f"Commande : `{' '.join(cmd)}`\n\n"
                    f"Dis 'confirme {app}' pour lancer (valide 5 minutes)."
                )
            )

        return self._launch(cmd, alias=app, description=description)

    def _confirm_pending(self, alias: str) -> ToolResult:
        now = datetime.now(UTC)
        expired = [k for k, p in self._pending.items() if p.expires_at <= now]
        for k in expired:
            del self._pending[k]

        pending = self._pending.pop(alias, None)
        if pending is None:
            return ToolResult(
                content=(
                    f"Aucune application '{alias}' en attente d'approbation "
                    "(ou délai de 5 minutes expiré). Relance la commande."
                ),
                is_error=True,
            )

        logger.info("LaunchApp confirmed", alias=alias)
        return self._launch(pending.cmd, alias=alias, description=pending.description)

    def _launch(self, cmd: list[str], alias: str, description: str) -> ToolResult:
        try:
            subprocess.Popen(  # noqa: S603
                cmd,
                creationflags=_DETACHED,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("LaunchApp launched", alias=alias, cmd=cmd)
            return ToolResult(content=f"'{description}' lancée.")
        except (FileNotFoundError, OSError) as e:
            return ToolResult(
                content=f"Erreur lors du lancement de '{description}' : {e}",
                is_error=True,
            )


__all__ = ["LaunchAppTool"]
