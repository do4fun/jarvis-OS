# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de la conversation téléphonique Twilio (SIP sortant/entrant, pipeline vocal).

Contient la suite pytest mockée (test_*.py, zéro appel réseau réel) qui couvre
chaque composant de la chaîne — cf. docs/superpowers/specs/
2026-07-04-twilio-telephony-e2e-tests-design.md — ainsi que les scripts manuels
`twilio_*.py` (hors convention pytest, appels réels à l'API Twilio, numéros en
argument/`.env`) utilisés pour un diagnostic ad hoc.
"""

from __future__ import annotations
