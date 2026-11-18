"""Text-to-speech. Kokoro-82M (local ONNX) with macOS `say` fallback. Interruptible."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path

import httpx

log = logging.getLogger("jarvis.tts")

KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def clean_for_speech(text: str, max_chars: int) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[#*_>|~]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        text = cut[: cut.rfind(". ") + 1] or cut
    return text


def download_kokoro(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        dest = model_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"have {dest}")
            continue
        print(f"downloading {name} ...")
        tmp = dest.with_suffix(".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.rename(dest)
    print("kokoro model ready")


class TTS:
    def __init__(self, cfg: dict):
        t = cfg["tts"]
        self.engine = t["engine"]
        self.voice = t["voice"]
        self.lang = t["lang"]
        self.speed = t["speed"]
        self.say_voice = t["say_voice"]
        self.max_chars = t["max_chars"]
        self.model_dir: Path = t["model_dir"]
        self._kokoro = None
        self._say_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self.speaking = False
        if self.engine == "kokoro":
            self._load_kokoro()

    def _load_kokoro(self) -> None:
        onnx = self.model_dir / "kokoro-v1.0.onnx"
        voices = self.model_dir / "voices-v1.0.bin"
        if not (onnx.exists() and voices.exists()):
            log.warning("kokoro model missing in %s; run `jarvis setup-tts`. Using say.", self.model_dir)
            self.engine = "say"
            return
        try:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(str(onnx), str(voices))
        except Exception as e:  # noqa: BLE001
            log.warning("kokoro load failed (%s); using say", e)
            self.engine = "say"

    def speak(self, text: str) -> None:
        """Blocking. Interrupted by stop()."""
        text = clean_for_speech(text, self.max_chars)
        if not text:
            return
        self._stopped.clear()
        with self._lock:
            if self._stopped.is_set():
                return
            self.speaking = True
            try:
                if self.engine == "kokoro" and self._kokoro is not None:
                    try:
                        self._speak_kokoro(text)
                        return
                    except Exception as e:  # noqa: BLE001
                        log.warning("kokoro speak failed (%s); falling back to say", e)
                self._speak_say(text)
            finally:
                self.speaking = False

    def _speak_kokoro(self, text: str) -> None:
        import sounddevice as sd

        # Sentence-chunk so the first words play fast and stop() is responsive.
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if self._stopped.is_set() or not sentence.strip():
                break
            samples, sr = self._kokoro.create(sentence, voice=self.voice, speed=self.speed, lang=self.lang)
            if self._stopped.is_set():
                break
            sd.play(samples, sr)
            sd.wait()

    def _speak_say(self, text: str) -> None:
        self._say_proc = subprocess.Popen(["say", "-v", self.say_voice, text])
        self._say_proc.wait()
        self._say_proc = None

    def stop(self) -> None:
        self._stopped.set()
        if self._say_proc and self._say_proc.poll() is None:
            self._say_proc.terminate()
        if self.engine == "kokoro":
            try:
                import sounddevice as sd

                sd.stop()
            except Exception:  # noqa: BLE001
                pass
