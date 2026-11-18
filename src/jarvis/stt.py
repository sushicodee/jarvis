"""Local speech-to-text. Backends: mlx-whisper (default) or the openai-whisper CLI."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .audio import save_wav

# Whisper's usual output on silence / noise.
HALLUCINATIONS = {
    "", "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "bye.", "bye", ".", "okay.", "so",
}


class STT:
    def __init__(self, cfg: dict):
        s = cfg["stt"]
        self.backend = s["backend"]
        self.mlx_model = s["mlx_model"]
        self.cli_model = s["cli_model"]
        self.language = s["language"]
        self.sr = cfg["audio"]["sample_rate"]

    def warmup(self) -> None:
        if self.backend == "mlx":
            self.transcribe(np.zeros(self.sr, np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        if self.backend == "mlx":
            text = self._mlx(audio)
        else:
            text = self._cli(audio)
        text = " ".join(text.split())
        return "" if text.lower() in HALLUCINATIONS else text

    def _mlx(self, audio: np.ndarray) -> str:
        import mlx_whisper

        res = mlx_whisper.transcribe(
            audio.astype(np.float32),
            path_or_hf_repo=self.mlx_model,
            language=self.language,
            condition_on_previous_text=False,
        )
        return res.get("text", "")

    def _cli(self, audio: np.ndarray) -> str:
        whisper = os.path.expanduser("~/.local/bin/whisper")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "in.wav"
            save_wav(wav, audio, self.sr)
            subprocess.run(
                [whisper, str(wav), "--model", self.cli_model, "--language", self.language,
                 "--output_format", "txt", "--output_dir", tmp, "--fp16", "False", "--verbose", "False"],
                check=True, capture_output=True,
            )
            return (Path(tmp) / "in.txt").read_text()
