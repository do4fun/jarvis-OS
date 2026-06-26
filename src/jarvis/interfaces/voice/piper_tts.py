# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Adaptateur TTS LiveKit pour Piper — synthèse vocale locale, zéro API."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from livekit.agents import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS, tts
from livekit.agents.tts import AudioEmitter, ChunkedStream, TTSCapabilities

from jarvis.kernel.paths import PROJECT_ROOT


def _find_piper_exe() -> str:
    """Cherche l'exécutable piper dans le bundle venv ou le PATH."""
    candidates = [
        Path(sys.executable).parent / "piper.exe",   # bundle/.venv/Scripts/
        Path(sys.executable).parent / "piper",        # Unix venv/bin/
        PROJECT_ROOT / "bundle" / ".venv" / "Scripts" / "piper.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return "piper"  # espère qu'il est dans le PATH


def _read_sample_rate(model_path: str) -> int:
    """Lit le sample_rate depuis le .json Piper associé au .onnx."""
    cfg = Path(model_path + ".json")
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return int(data.get("audio", {}).get("sample_rate", 22050))
        except Exception:
            pass
    return 22050  # valeur par défaut Piper


class _PiperChunkedStream(ChunkedStream):
    """Implémentation ChunkedStream qui appelle piper en sous-processus."""

    async def _run(self, output_emitter: AudioEmitter) -> None:
        piper_exe = self._tts._piper_exe  # type: ignore[attr-defined]
        model = self._tts._model_path     # type: ignore[attr-defined]
        sample_rate = self._tts._sample_rate

        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )

        proc = await asyncio.create_subprocess_exec(
            piper_exe,
            "--model", model,
            "--output_raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(self.input_text.encode("utf-8")),
            timeout=30.0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"piper exited with code {proc.returncode}")
        output_emitter.push(stdout)


class PiperTTS(tts.TTS):
    """TTS LiveKit utilisant Piper en local — aucune clé API requise."""

    def __init__(self, model_path: str) -> None:
        sample_rate = _read_sample_rate(model_path)
        super().__init__(
            capabilities=TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._model_path = str(Path(model_path).resolve())
        self._piper_exe = _find_piper_exe()

    @property
    def model(self) -> str:
        return Path(self._model_path).stem

    @property
    def provider(self) -> str:
        return "piper-local"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _PiperChunkedStream:
        return _PiperChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        pass
