from __future__ import annotations

import json

from jarvis.interfaces.openclaw.session_map import OpenClawSessionMap


def test_set_puis_get(tmp_path) -> None:
    store = OpenClawSessionMap(tmp_path / "openclaw_sessions.json")
    assert store.get("openclaw:abc") is None
    store.set("openclaw:abc", "sess-1")
    assert store.get("openclaw:abc") == "sess-1"


def test_persiste_sur_disque(tmp_path) -> None:
    path = tmp_path / "openclaw_sessions.json"
    OpenClawSessionMap(path).set("openclaw:abc", "sess-1")
    reloaded = OpenClawSessionMap(path)
    assert reloaded.get("openclaw:abc") == "sess-1"
    assert json.loads(path.read_text()) == {"openclaw:abc": "sess-1"}
