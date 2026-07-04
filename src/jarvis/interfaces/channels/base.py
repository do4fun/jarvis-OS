# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Abstractions de base pour les canaux de messagerie Jarvis.

Définit :
  - Platform : enum des plateformes supportées
  - IncomingMessage : message normalisé entrant
  - MessageTarget : cible de réponse normalisée
  - ChannelAdapter : ABC des adaptateurs
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Platform(StrEnum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    MESSENGER = "messenger"
    SIGNAL = "signal"
    SLACK = "slack"


@dataclass(frozen=True)
class IncomingMessage:
    """Message entrant normalisé depuis n'importe quelle plateforme."""

    platform: Platform
    user_id: str
    text: str
    channel_id: str = ""
    raw: object = field(default=None, hash=False, compare=False)
    identity_key: str | None = None

    @property
    def session_key(self) -> str:
        """Clé de session unique.

        Par défaut 'platform:user_id'. Un adaptateur peut fournir `identity_key`
        pour unifier le contexte à travers plusieurs canaux qui partagent la
        même identité réelle (ex. numéro E.164 Twilio partagé entre WhatsApp
        et la voix) — cf. docs/architecture/2026-06-28-twilio-multicanal-v2.md §4.1.
        """
        return self.identity_key or f"{self.platform.value}:{self.user_id}"


@dataclass(frozen=True)
class MessageTarget:
    """Cible à qui envoyer une réponse."""

    platform: Platform
    user_id: str
    channel_id: str = ""


# Signature du callback de dispatch injecté par MessagingGateway.
DispatchCallback = Callable[[IncomingMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    """Interface commune pour tous les canaux de messagerie.

    Un adaptateur :
    - normalise les messages entrants en IncomingMessage
    - les transmet au callback fourni par set_dispatch()
    - envoie les réponses via send()
    """

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Identifiant de la plateforme principale (utilisé par défaut par `platforms`)."""

    @property
    def platforms(self) -> tuple[Platform, ...]:
        """Plateformes servies par cet adaptateur — un seul adaptateur peut en
        couvrir plusieurs (ex. TwilioMessagingChannel : whatsapp + messenger).
        Par défaut, une seule (`platform`) ; à surcharger si besoin."""
        return (self.platform,)

    @abstractmethod
    async def start(self) -> None:
        """Démarre l'écoute des messages entrants."""

    @abstractmethod
    async def stop(self) -> None:
        """Arrête proprement le canal."""

    @abstractmethod
    async def send(self, reply: str, target: MessageTarget) -> None:
        """Envoie une réponse à la cible donnée."""

    def set_dispatch(self, callback: DispatchCallback) -> None:
        """Injecte le callback de dispatch appelé à chaque message entrant."""
        self._dispatch_cb: DispatchCallback | None = callback
