# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.kernel.paths import PROJECT_ROOT

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFJ]")
_TESTS_DIR = PROJECT_ROOT / "tests"
_MAX_OUTPUT_CHARS = 6000


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_pytest_summary(output: str) -> str:
    """Extrait la ligne de résumé pytest (ex: '3 passed, 1 failed in 2.45s')."""
    for line in reversed(output.splitlines()):
        cleaned = _strip_ansi(line).strip()
        if any(kw in cleaned for kw in ("passed", "failed", "error", "no tests ran")):
            return cleaned
    return "aucun résumé disponible"


def _list_test_files() -> list[str]:
    return sorted(p.name for p in _TESTS_DIR.glob("test_*.py"))


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "Lance les tests pytest du projet Jarvis pour diagnostiquer un bug ou vérifier "
        "le bon fonctionnement d'un module. "
        "Paramètres optionnels: test_file (nom du fichier dans tests/, ex: 'test_tools.py'), "
        "filter (expression pytest -k, ex: 'cli'), list_only (true = liste les tests sans les exécuter). "
        "Sans test_file, lance TOUS les tests (lent). "
        "Retourne un résumé passé/échoué et le détail des erreurs."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "test_file": {
                "type": "string",
                "description": (
                    "Fichier de test relatif à tests/ (ex: 'test_tools.py'). "
                    "Omis = tous les tests."
                ),
            },
            "filter": {
                "type": "string",
                "description": "Expression pytest -k pour cibler des tests spécifiques (ex: 'cli or memory').",
            },
            "list_only": {
                "type": "boolean",
                "description": "true = liste les fichiers de tests disponibles sans les exécuter.",
            },
        },
        "required": [],
    }

    async def execute(
        self,
        test_file: str | None = None,
        filter: str | None = None,  # noqa: A002
        list_only: bool = False,
        **_: object,
    ) -> ToolResult:
        if not _TESTS_DIR.exists():
            return ToolResult(content="Répertoire tests/ introuvable à la racine du projet.", is_error=True)

        if list_only:
            files = _list_test_files()
            listing = "\n".join(f"  - {f}" for f in files)
            return ToolResult(content=f"Fichiers de tests disponibles ({len(files)}) :\n{listing}")

        # Resolve target
        target: Path | None = None
        if test_file:
            candidate = _TESTS_DIR / (test_file if test_file.endswith(".py") else test_file + ".py")
            if not candidate.exists():
                available = ", ".join(_list_test_files()[:10])
                return ToolResult(
                    content=f"Fichier introuvable: {test_file}\nFichiers disponibles: {available} …",
                    is_error=True,
                )
            target = candidate

        cmd = [
            sys.executable, "-m", "pytest",
            "--tb=short",
            "-v",
            "--no-header",
            str(target) if target else str(_TESTS_DIR),
        ]
        if filter:
            cmd += ["-k", filter]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            return ToolResult(content="Timeout : les tests ont dépassé 120 secondes.", is_error=True)
        except Exception as exc:
            return ToolResult(content=f"Erreur lors du lancement de pytest : {exc}", is_error=True)

        raw = _strip_ansi(stdout.decode("utf-8", errors="replace"))
        summary = _parse_pytest_summary(raw)
        success = proc.returncode == 0

        # Keep only the most informative lines when output is long
        if len(raw) > _MAX_OUTPUT_CHARS:
            raw = raw[:_MAX_OUTPUT_CHARS] + f"\n… [tronqué — {len(raw)} caractères au total]"

        return ToolResult(
            content=f"**{summary}**\n\n```\n{raw.strip()}\n```",
            is_error=not success,
        )
