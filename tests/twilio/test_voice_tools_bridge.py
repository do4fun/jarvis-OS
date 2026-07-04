# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de _make_livekit_tool — pont entre un Tool Jarvis et un RawFunctionTool
LiveKit utilisé par le pipeline vocal.

Couvre : conversion fidèle du schema (name/description/parameters) et
préfixage `[ERREUR]` du contenu quand le Tool sous-jacent renvoie
`ToolResult(is_error=True)`.
"""

from __future__ import annotations

import pytest

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.interfaces.voice.agent import _make_livekit_tool


class _FakeTool(Tool):
    name = "fake_tool"
    description = "Un outil factice pour les tests du pont LiveKit."
    input_schema = {  # noqa: RUF012
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.received: dict | None = None

    async def execute(self, **kwargs: object) -> ToolResult:
        self.received = kwargs
        return self._result


def test_make_livekit_tool_convertit_le_schema() -> None:
    tool = _FakeTool(ToolResult(content="ok"))
    raw_tool = _make_livekit_tool(tool)

    assert raw_tool._info.name == "fake_tool"
    assert raw_tool._info.raw_schema["description"] == _FakeTool.description
    assert raw_tool._info.raw_schema["parameters"] == _FakeTool.input_schema


@pytest.mark.asyncio
async def test_execute_prefixe_erreur() -> None:
    tool = _FakeTool(ToolResult(content="trunk indisponible", is_error=True))
    raw_tool = _make_livekit_tool(tool)

    result = await raw_tool({"x": "y"})

    assert result == "[ERREUR] trunk indisponible"
    assert tool.received == {"x": "y"}


@pytest.mark.asyncio
async def test_execute_passe_le_contenu_si_ok() -> None:
    tool = _FakeTool(ToolResult(content="Appel initié."))
    raw_tool = _make_livekit_tool(tool)

    result = await raw_tool({"x": "y"})

    assert result == "Appel initié."
