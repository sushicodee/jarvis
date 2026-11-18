"""Daemon: localhost HTTP control surface + mic recorder + pipeline.

POST /toggle  start recording, or stop and process (also interrupts speech)
POST /start   start recording
POST /stop    stop recording and process
POST /cancel  discard recording, stop speaking
POST /text    body = plain text command (skips STT)
GET  /status  JSON state
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audio import Recorder, rms
from .brain import Brain
from .stt import STT
from .tts import TTS

log = logging.getLogger("jarvis.server")


class Jarvis:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tts = TTS(cfg)
        self.stt = STT(cfg)
        self.brain = Brain(cfg, self.tts, on_ask=self.listen_for_answer)
        self.recorder = Recorder(cfg, on_autostop=self.stop_and_process)
        self.state = "idle"
        self.last_heard = ""
        self._mic_warned = False

    def warmup(self) -> None:
        log.info("warming STT (%s)...", self.stt.backend)
        self.stt.warmup()
        log.info("ready")

    def toggle(self) -> str:
        if self.recorder.recording:
            threading.Thread(target=self.stop_and_process, daemon=True).start()
            return "stopping"
        self.tts.stop()
        self.recorder.start()
        self.state = "listening"
        return "listening"

    def start(self) -> str:
        if not self.recorder.recording:
            self.tts.stop()
            self.recorder.start()
            self.state = "listening"
        return self.state

    def listen_for_answer(self) -> None:
        """Open the mic after Jarvis asks a question; it closes on its own if nobody answers."""
        if not self.recorder.recording:
            self.recorder.start(no_speech_timeout=self.cfg["disk"]["answer_listen_seconds"])
            self.state = "listening"

    def cancel(self) -> str:
        self.brain.cancel_pending()
        self.recorder.stop()
        self.tts.stop()
        self.state = "idle"
        return "cancelled"

    def stop_and_process(self) -> None:
        audio = self.recorder.stop()
        if audio is None:
            return
        level = rms(audio)
        if audio.size == 0 or level == 0.0:
            self.state = "idle"
            if not self._mic_warned:
                self._mic_warned = True
                self.tts.speak("I'm getting no audio. Check microphone permission for the terminal running me.")
            return
        if level < self.cfg["audio"]["silence_rms"] / 3:
            self.state = "idle"
            return
        self.state = "transcribing"
        text = self.stt.transcribe(audio)
        self.process_text(text)

    def process_text(self, text: str) -> dict:
        text = text.strip()
        if not text:
            self.state = "idle"
            return {"heard": "", "reply": ""}
        self.last_heard = text
        self.state = "thinking"
        try:
            res = self.brain.handle(text)
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed")
            self.tts.speak("Something broke. Check the Jarvis log.")
            self.state = "idle"
            return {"heard": text, "error": str(e)}
        self.state = "idle"
        if res.pending:
            self.tts.speak(res.reply)
        return {"heard": text, "tier": res.route.tier, "command": res.route.command,
                "context": res.route.context, "reply": res.reply, "pending": res.pending}

    def status(self) -> dict:
        state = self.state
        if self.recorder.recording:
            state = "listening"
        elif self.tts.speaking:
            state = "speaking"
        elif state == "idle" and self.brain.pending:
            state = "confirm"
        elif state == "idle" and self.brain.jobs:
            state = "working"
        return {"state": state, "recording": self.recorder.recording,
                "level": round(self.recorder.level, 4) if self.recorder.recording else 0.0,
                "last_heard": self.last_heard, "last": self.brain.last,
                "running_jobs": list(self.brain.jobs.values()),
                "pending_confirm": self.brain.pending.summary if self.brain.pending else None,
                "stt": self.stt.backend, "tts": self.tts.engine}


def serve(cfg: dict) -> None:
    jarvis = Jarvis(cfg)
    jarvis.warmup()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/status":
                return self._send(200, jarvis.status())
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path == "/toggle":
                return self._send(200, {"state": jarvis.toggle()})
            if self.path == "/start":
                return self._send(200, {"state": jarvis.start()})
            if self.path == "/stop":
                threading.Thread(target=jarvis.stop_and_process, daemon=True).start()
                return self._send(200, {"state": "stopping"})
            if self.path == "/cancel":
                return self._send(200, {"state": jarvis.cancel()})
            if self.path == "/text":
                n = int(self.headers.get("Content-Length", 0))
                text = self.rfile.read(n).decode()
                return self._send(200, jarvis.process_text(text))
            self._send(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    host, port = cfg["server"]["host"], cfg["server"]["port"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("jarvis listening on http://%s:%s", host, port)
    jarvis.tts.speak("Jarvis online.")
    httpd.serve_forever()
