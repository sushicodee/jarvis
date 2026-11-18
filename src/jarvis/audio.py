"""Microphone capture: start/stop recorder with optional silence auto-stop."""

from __future__ import annotations

import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

EARCON_START = "/System/Library/Sounds/Tink.aiff"
EARCON_STOP = "/System/Library/Sounds/Pop.aiff"


def earcon(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Recorder:
    def __init__(self, cfg: dict, on_autostop: Callable[[], None] | None = None):
        a = cfg["audio"]
        self.sr = a["sample_rate"]
        self.max_seconds = a["max_seconds"]
        self.silence_seconds = a["silence_seconds"]
        self.silence_rms = a["silence_rms"]
        self.earcons = a["earcons"]
        self.on_autostop = on_autostop
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._started = 0.0
        self._had_speech = False
        self._last_voice = 0.0
        self._autostop_fired = False
        self._no_speech_timeout: float | None = None
        self.level = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, t, status) -> None:
        chunk = indata[:, 0].copy()
        self._chunks.append(chunk)
        now = time.monotonic()
        self.level = float(np.sqrt(np.mean(chunk**2)))
        if self.level > self.silence_rms:
            self._had_speech = True
            self._last_voice = now
        over_max = now - self._started > self.max_seconds
        silent_tail = (
            self.silence_seconds > 0
            and self._had_speech
            and now - self._last_voice > self.silence_seconds
        )
        no_answer = (
            self._no_speech_timeout is not None
            and not self._had_speech
            and now - self._started > self._no_speech_timeout
        )
        if (over_max or silent_tail or no_answer) and not self._autostop_fired and self.on_autostop:
            self._autostop_fired = True
            threading.Thread(target=self.on_autostop, daemon=True).start()

    def start(self, no_speech_timeout: float | None = None) -> None:
        with self._lock:
            if self._stream:
                return
            self._no_speech_timeout = no_speech_timeout
            self._chunks = []
            self._had_speech = False
            self._autostop_fired = False
            self._started = self._last_voice = time.monotonic()
            self._stream = sd.InputStream(
                samplerate=self.sr, channels=1, dtype="float32", callback=self._callback
            )
            self._stream.start()
        if self.earcons:
            earcon(EARCON_START)

    def stop(self) -> np.ndarray | None:
        """Stop and return mono float32 audio, or None if not recording."""
        with self._lock:
            if not self._stream:
                return None
            self._stream.stop()
            self._stream.close()
            self._stream = None
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        if self.earcons:
            earcon(EARCON_STOP)
        return audio


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0


def load_wav(path: Path, target_sr: int = 16000) -> np.ndarray:
    """Load any audio file as mono float32 at target_sr via ffmpeg."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-ac", "1", "-ar", str(target_sr), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, np.float32).copy()


def save_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
