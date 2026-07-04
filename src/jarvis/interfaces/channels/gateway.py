# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Gateway de messagerie unifié Jarvis.

MessagingGateway orchestre N ChannelAdapters, assure la continuité de session
cross-plateforme en persistant un mapping (platform:user_id → session_id) dans
un SessionKeyStore SQLite, et route chaque message entrant vers le core.Gateway
Jarvis.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from jarvis.engine.gateway import Gateway as JarvisGateway
from jarvis.interfaces.channels.base import ChannelAdapter, IncomingMessage, MessageTarget
from jarvis.kernel.session_key_store import SessionKeyStore

_SESSION_DB_FILE = Path("memory/messaging_sessions.db")


class MessagingGateway:
    """Orchestre plusieurs ChannelAdapters avec continuité de session.

    Usage::

        gw = MessagingGateway(jarvis_gateway=app.state.gateway)
        gw.register(TelegramChannel(...))
        gw.register(DiscordChannel(...))
        await gw.start_all()
        # ...
        await gw.stop_all()
    """

    def __init__(
        self,
        jarvis_gateway: JarvisGateway,
        session_db_path: Path = _SESSION_DB_FILE,
        session_key_store: SessionKeyStore | None = None,
    ) -> None:
        self._jarvis = jarvis_gateway
        self._adapters: dict[str, ChannelAdapter] = {}
        self._store = session_key_store or SessionKeyStore(session_db_path)

    # ── Gestion des adaptateurs ───────────────────────────────────────────────

    def register(self, adapter: ChannelAdapter) -> None:
        """Enregistre un adaptateur et lui injecte le callback de dispatch.

        Un adaptateur peut servir plusieurs plateformes (ex. TwilioMessagingChannel
        pour whatsapp + messenger) — il est indexé sous chacune via `adapter.platforms`."""
        adapter.set_dispatch(self.dispatch)
        for plat in adapter.platforms:
            self._adapters[plat.value] = adapter
        logger.info("Canal enregistré", platforms=[p.value for p in adapter.platforms])

    async def start_all(self) -> None:
        """Démarre tous les adaptateurs enregistrés (dédupliqués — un adaptateur
        multi-plateforme apparaît sous plusieurs clés de `_adapters`)."""
        for adapter in {id(a): a for a in self._adapters.values()}.values():
            await adapter.start()

    async def stop_all(self) -> None:
        """Arrête proprement tous les adaptateurs (dédupliqués)."""
        for adapter in {id(a): a for a in self._adapters.values()}.values():
            await adapter.stop()

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def dispatch(self, msg: IncomingMessage) -> None:
        """Route un message entrant vers Jarvis et renvoie la réponse au bon canal.

        La session est restaurée depuis le mapping persisté si elle existe,
        ou créée à la volée par le core.Gateway.
        """
        session_id = self._store.get(msg.session_key)

        logger.debug(
            "Dispatch message",
            platform=msg.platform.value,
            user_id=msg.user_id,
            session_id=session_id,
            text=msg.text[:60],
        )

        session, _route, response = await self._jarvis.handle(
            msg.text,
            session_id=session_id,
            stream=False,
        )

        # Persiste le session_id (nouveau ou restauré) — SQLite WAL, écriture
        # concurrente sûre entre canaux (cf. audit §2.2).
        self._store.persist(msg.session_key, str(session.id))

        adapter = self._adapters.get(msg.platform.value)
        if adapter is None:
            logger.warning("Adaptateur introuvable pour la réponse", platform=msg.platform.value)
            return

        target = MessageTarget(
            platform=msg.platform,
            user_id=msg.user_id,
            channel_id=msg.channel_id,
        )
        await adapter.send(str(response), target)

    @property
    def session_key_store(self) -> SessionKeyStore:
        """Exposé pour que d'autres interfaces (voix Twilio) partagent le même store."""
        return self._store
