# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Store de session partagé cross-canal — SQLite en mode WAL.

Remplace le mapping JSON non verrouillé de MessagingGateway (cf. audit
docs/architecture/2026-06-28-twilio-multicanal-debat.md §2.2) : deux
événements concurrents pour la même identité (ex. un message WhatsApp et
un appel qui se terminent au même instant) ne peuvent plus s'écraser
mutuellement ni corrompre le fichier — le moteur SQLite gère la
concurrence en écriture, pas un lock applicatif maison.

Sert aussi de base à l'unification de contexte voix ⇄ écrit (§4.1 du
plan v2) : une clé d'identité normalisée (ex. "twilio:+15145551234")
partagée entre `MessagingGateway` (WhatsApp/Messenger) et le pipeline
vocal (appels Twilio via LiveKit SIP) pointe vers le même `session_id`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class SessionKeyStore:
    """Mapping durable `identity_key -> session_id` + horodatage du dernier message entrant.

    Un seul fichier SQLite pour tous les canaux (Telegram, Discord, Twilio…) —
    remplace `memory/messaging_sessions.json`.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_keys (
                identity_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                last_inbound_at TEXT
            )
            """
        )
        self._conn.commit()

    def get(self, identity_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM session_keys WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        return row[0] if row else None

    def resolve_or_create(
        self, identity_key: str, new_session_id_factory: Callable[[], str]
    ) -> str:
        existing = self.get(identity_key)
        if existing is not None:
            return existing
        session_id = new_session_id_factory()
        self.persist(identity_key, session_id)
        return session_id

    def persist(self, identity_key: str, session_id: str) -> None:
        """Insère ou met à jour le `session_id` associé à une identité."""
        self._conn.execute(
            """
            INSERT INTO session_keys (identity_key, session_id) VALUES (?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET session_id = excluded.session_id
            """,
            (identity_key, session_id),
        )
        self._conn.commit()

    def mark_inbound(self, identity_key: str, ts: datetime) -> None:
        """Horodate le dernier message entrant — utilisé pour la fenêtre 24h WhatsApp."""
        self._conn.execute(
            """
            INSERT INTO session_keys (identity_key, session_id, last_inbound_at)
            VALUES (?, '', ?)
            ON CONFLICT(identity_key) DO UPDATE SET last_inbound_at = excluded.last_inbound_at
            """,
            (identity_key, ts.isoformat()),
        )
        self._conn.commit()

    def last_inbound_at(self, identity_key: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT last_inbound_at FROM session_keys WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    def close(self) -> None:
        self._conn.close()


__all__ = ["SessionKeyStore"]
