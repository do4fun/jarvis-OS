# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Wiring OpenClaw au boot du process API — hors-Container par design,
même statut que `interfaces/channels/setup.py::setup_channels` (CDC §C.1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from loguru import logger

from jarvis.interfaces.api.openclaw import router as openclaw_router
from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap
from jarvis.kernel.settings import settings
from jarvis.providers.openclaw.client import OpenClawClient

if TYPE_CHECKING:
    from jarvis.bootstrap import Container


async def setup_openclaw(app: FastAPI, container: Container) -> OpenClawClient | None:
    """Construit et démarre le client OpenClaw selon le flag env `OPENCLAW_ENABLED`.

    Retourne le `OpenClawClient` instancié (attaché à `app.state.openclaw_client`)
    ou `None` si le flag est absent/désactivé — aucun effet de bord dans ce cas,
    à l'image de `setup_channels`.
    """
    if os.getenv("OPENCLAW_ENABLED", "false").lower() != "true":
        return None

    ws_url = os.environ["OPENCLAW_WS_URL"]
    token = os.environ["OPENCLAW_GATEWAY_TOKEN"]

    client = OpenClawClient(ws_url=ws_url, token=token)
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001 — tolérant : Gateway peut démarrer après Jarvis
        logger.warning("OpenClaw Gateway injoignable au boot", error=str(exc))

    memory_dir = Path(settings.memory_dir)
    app.state.container = container
    app.state.openclaw_client = client
    app.state.openclaw_session_map = OpenClawSessionMap(memory_dir / "openclaw_sessions.json")
    app.include_router(openclaw_router)
    logger.info("OpenClaw wiring actif", ws_url=ws_url)
    return client
