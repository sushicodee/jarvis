"""CLI entry point: `jarvis <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from .config import load_config


def _daemon(cfg: dict, method: str, path: str, data: str | None = None) -> None:
    url = f"http://{cfg['server']['host']}:{cfg['server']['port']}{path}"
    try:
        r = httpx.request(method, url, content=data, timeout=120)
        print(json.dumps(r.json(), indent=2))
    except httpx.ConnectError:
        sys.exit("jarvis daemon not running (start: pm2 start ecosystem.config.cjs)")


def main() -> None:
    p = argparse.ArgumentParser(prog="jarvis")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the daemon")
    for name in ("toggle", "start", "stop", "cancel", "status"):
        sub.add_parser(name, help=f"daemon: {name}")
    s = sub.add_parser("send", help="daemon: send a text command")
    s.add_argument("text")
    t = sub.add_parser("text", help="run a text command locally (no daemon), wait for result")
    t.add_argument("text")
    t.add_argument("--quiet", action="store_true", help="don't speak")
    t.add_argument("--route-only", action="store_true", help="print the Jev route and exit")
    f = sub.add_parser("file", help="transcribe an audio file and run it")
    f.add_argument("path")
    f.add_argument("--quiet", action="store_true")
    f.add_argument("--transcribe-only", action="store_true")
    sy = sub.add_parser("say", help="speak text with the configured TTS")
    sy.add_argument("text")
    sub.add_parser("setup-tts", help="download Kokoro model files")
    sub.add_parser("inbox", help="process vault inbox/ with Claude Code")
    b = sub.add_parser("bench", help="benchmark STT backends on an audio file")
    b.add_argument("path")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()

    if args.cmd == "serve":
        from .server import serve
        serve(cfg)
    elif args.cmd in ("toggle", "start", "stop", "cancel"):
        _daemon(cfg, "POST", f"/{args.cmd}")
    elif args.cmd == "status":
        _daemon(cfg, "GET", "/status")
    elif args.cmd == "send":
        _daemon(cfg, "POST", "/text", args.text)
    elif args.cmd == "text":
        _run_text(cfg, args.text, args.quiet, args.route_only)
    elif args.cmd == "file":
        from .audio import load_wav
        from .stt import STT
        audio = load_wav(Path(args.path), cfg["audio"]["sample_rate"])
        t0 = time.perf_counter()
        text = STT(cfg).transcribe(audio)
        print(f"heard ({time.perf_counter() - t0:.2f}s): {text!r}")
        if not args.transcribe_only and text:
            _run_text(cfg, text, args.quiet, False)
    elif args.cmd == "say":
        from .tts import TTS
        TTS(cfg).speak(args.text)
    elif args.cmd == "setup-tts":
        from .tts import download_kokoro
        download_kokoro(cfg["tts"]["model_dir"])
    elif args.cmd == "inbox":
        from .brain import Brain
        print(Brain(cfg, None).process_inbox())
    elif args.cmd == "bench":
        _bench(cfg, Path(args.path))


def _run_text(cfg: dict, text: str, quiet: bool, route_only: bool) -> None:
    from .brain import Brain
    from .tts import TTS
    brain = Brain(cfg, None if quiet else TTS(cfg))
    if route_only:
        r = brain.router.route(text)
        print(json.dumps({"tier": r.tier, "command": r.command, "context": r.context,
                          "confidence": round(r.confidence, 3)}))
        return
    res = brain.handle(text, wait=True)
    print(json.dumps({"tier": res.route.tier, "command": res.route.command,
                      "context": res.route.context, "reply": res.reply}, indent=2))


def _bench(cfg: dict, path: Path) -> None:
    from .audio import load_wav
    from .stt import STT
    audio = load_wav(path, cfg["audio"]["sample_rate"])
    dur = audio.size / cfg["audio"]["sample_rate"]
    runs = [("mlx", cfg["stt"]["mlx_model"]), ("mlx", "mlx-community/whisper-small.en-mlx"),
            ("whisper-cli", cfg["stt"]["cli_model"])]
    for backend, model in runs:
        c = {**cfg, "stt": {**cfg["stt"], "backend": backend}}
        if backend == "mlx":
            c["stt"]["mlx_model"] = model
        stt = STT(c)
        stt.warmup()
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        dt = time.perf_counter() - t0
        print(f"{backend:12} {model:42} {dt:6.2f}s  ({dur / dt:5.1f}x RT)  {text!r}")


if __name__ == "__main__":
    main()
