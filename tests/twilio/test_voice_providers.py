# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de sélection STT/TTS/LLM du pipeline vocal LiveKit (_build_voice_*).

Ces fonctions prennent un `env: dict` explicite (pas le singleton `settings`)
mais retombent sur `os.getenv(...)` quand une clé est absente du dict — les
tests neutralisent donc aussi l'environnement process pour rester
déterministes. Toutes les classes de plugins LiveKit (Deepgram, ElevenLabs,
OpenAI, Google, Anthropic) sont mockées : aucun client réseau réel n'est
construit.
"""

from __future__ import annotations

import logging
import sys

import livekit.plugins
import livekit.plugins.anthropic as lk_anthropic_mod
import livekit.plugins.openai as lk_openai_mod
import pytest

from jarvis.interfaces.voice import agent

_ENV_KEYS = (
    "STT_PROVIDER",
    "TTS_PROVIDER",
    "API_BACKEND",
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "QUEBEC_MODE",
    "VOICE_LLM_MODEL",
    "PIPER_MODEL_PATH",
)


@pytest.fixture(autouse=True)
def _clean_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_voice_* retombent sur os.getenv(...) si la clé n'est pas dans
    `env` — on neutralise l'environnement process pour ne dépendre que du
    dict `env` passé explicitement par chaque test."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _Recorder:
    """Classe de plugin factice qui capture ses kwargs de construction."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


def _recorder_class() -> type[_Recorder]:
    return type("_Recorder", (_Recorder,), {})


# ── STT ──────────────────────────────────────────────────────────────────


def test_stt_provider_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(lk_openai_mod, "STT", fake)

    result = agent._build_voice_stt({"STT_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"})

    assert isinstance(result, fake)
    assert result.kwargs["model"] == "gpt-4o-mini-transcribe"
    assert result.kwargs["language"] == "fr"
    assert result.kwargs["api_key"] == "sk-test"


def test_stt_provider_google(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(agent.lk_google, "STT", fake)

    result = agent._build_voice_stt({"STT_PROVIDER": "google"})

    assert isinstance(result, fake)
    assert result.kwargs["languages"] == "fr-FR"
    assert result.kwargs["model"] == "latest_long"


def test_stt_deepgram_defaut_cle_absente_logge_erreur(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(agent.deepgram, "STT", fake)

    with caplog.at_level(logging.ERROR, logger="jarvis-voice"):
        result = agent._build_voice_stt({})

    assert isinstance(result, fake)
    assert result.kwargs["model"] == "nova-2"
    assert any("DEEPGRAM_API_KEY" in rec.message for rec in caplog.records)


def test_stt_provider_en_echec_replie_sur_deepgram(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("auth invalide")

    fake_deepgram = _recorder_class()
    monkeypatch.setattr(lk_openai_mod, "STT", _boom)
    monkeypatch.setattr(agent.deepgram, "STT", fake_deepgram)

    with caplog.at_level(logging.WARNING, logger="jarvis-voice"):
        result = agent._build_voice_stt(
            {"STT_PROVIDER": "openai", "OPENAI_API_KEY": "x", "DEEPGRAM_API_KEY": "d" * 24}
        )

    assert isinstance(result, fake_deepgram)
    assert any("indisponible" in rec.message for rec in caplog.records)


# ── TTS ──────────────────────────────────────────────────────────────────


def test_tts_gemini_avec_repli_elevenlabs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini = _recorder_class()
    fake_eleven = _recorder_class()
    fake_fallback = _recorder_class()
    monkeypatch.setattr(agent.gemini_tts, "TTS", fake_gemini)
    monkeypatch.setattr(agent.elevenlabs, "TTS", fake_eleven)
    monkeypatch.setattr(agent.tts, "FallbackAdapter", fake_fallback)

    result = agent._build_voice_tts(
        {"TTS_PROVIDER": "gemini", "GOOGLE_API_KEY": "g", "ELEVENLABS_API_KEY": "e"}
    )

    assert isinstance(result, fake_fallback)
    adapters = result.args[0]
    assert len(adapters) == 2
    assert isinstance(adapters[0], fake_gemini)
    assert isinstance(adapters[1], fake_eleven)


def test_tts_gemini_sans_repli_avertit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_gemini = _recorder_class()
    monkeypatch.setattr(agent.gemini_tts, "TTS", fake_gemini)

    with caplog.at_level(logging.WARNING, logger="jarvis-voice"):
        result = agent._build_voice_tts({"TTS_PROVIDER": "gemini", "GOOGLE_API_KEY": "g"})

    assert isinstance(result, fake_gemini)
    assert any("SANS repli" in rec.message for rec in caplog.records)


def test_tts_elevenlabs_seul(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_eleven = _recorder_class()
    monkeypatch.setattr(agent.elevenlabs, "TTS", fake_eleven)

    result = agent._build_voice_tts({"TTS_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": "e"})

    assert isinstance(result, fake_eleven)


def test_tts_openai_seul(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_openai = _recorder_class()
    monkeypatch.setattr(lk_openai_mod, "TTS", fake_openai)

    result = agent._build_voice_tts({"TTS_PROVIDER": "openai", "OPENAI_API_KEY": "o"})

    assert isinstance(result, fake_openai)
    assert result.kwargs["voice"] == "alloy"
    assert result.kwargs["api_key"] == "o"


def test_tts_aucune_cle_leve_valueerror() -> None:
    with pytest.raises(ValueError):
        agent._build_voice_tts({"TTS_PROVIDER": "inconnu"})


def test_tts_piper_modele_absent_replie_sur_elevenlabs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_eleven = _recorder_class()
    monkeypatch.setattr(agent.elevenlabs, "TTS", fake_eleven)

    with caplog.at_level(logging.WARNING, logger="jarvis-voice"):
        result = agent._build_voice_tts(
            {
                "TTS_PROVIDER": "piper",
                "ELEVENLABS_API_KEY": "e",
                "PIPER_MODEL_PATH": "models/piper/__inexistant__.onnx",
            }
        )

    assert isinstance(result, fake_eleven)
    assert any("introuvable" in rec.message for rec in caplog.records)


# ── LLM ──────────────────────────────────────────────────────────────────


def test_llm_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(lk_openai_mod, "LLM", fake)

    result = agent._build_voice_llm({"API_BACKEND": "openai"})

    assert isinstance(result, fake)
    assert result.kwargs["model"] == "gpt-4o-mini"
    assert result.kwargs["temperature"] == 0.7


def test_llm_mistral(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(lk_openai_mod, "LLM", fake)

    result = agent._build_voice_llm({"API_BACKEND": "mistral", "MISTRAL_API_KEY": "m-key"})

    assert isinstance(result, fake)
    assert result.kwargs["model"] == "mistral-large-latest"
    assert result.kwargs["base_url"] == "https://api.mistral.ai/v1"
    assert result.kwargs["api_key"] == "m-key"


def test_llm_anthropic_par_defaut(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _recorder_class()
    monkeypatch.setattr(lk_anthropic_mod, "LLM", fake)

    result = agent._build_voice_llm({})

    assert isinstance(result, fake)
    assert result.kwargs["model"] == "claude-haiku-4-5-20251001"


def test_llm_plugin_manquant_replie_sur_gemini(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_gemini = _recorder_class()
    monkeypatch.setattr(agent.lk_google, "LLM", fake_gemini)
    # `from livekit.plugins import anthropic` résout par getattr sur le package
    # déjà importé — il faut retirer l'attribut ET marquer le sous-module comme
    # absent dans sys.modules pour forcer un ImportError à l'import suivant.
    monkeypatch.delattr(livekit.plugins, "anthropic", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.anthropic", None)

    with caplog.at_level(logging.WARNING, logger="jarvis-voice"):
        result = agent._build_voice_llm({"API_BACKEND": "anthropic"})

    assert isinstance(result, fake_gemini)
    assert result.kwargs["model"] == "gemini-2.5-flash"
    assert any("manquant" in rec.message for rec in caplog.records)


def test_llm_backend_inconnu_gemini_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini = _recorder_class()
    monkeypatch.setattr(agent.lk_google, "LLM", fake_gemini)

    result = agent._build_voice_llm({"API_BACKEND": "inconnu"})

    assert isinstance(result, fake_gemini)
