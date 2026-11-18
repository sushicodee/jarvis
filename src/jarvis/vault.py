"""Obsidian vault I/O: daily Jarvis log, tier-3 outputs, inbox notes."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path


class Vault:
    def __init__(self, cfg: dict):
        v = cfg["vault"]
        self.root: Path = v["path"]
        self.log_dir = self.root / v["log_dir"]
        self.output_dir = self.root / v["output_dir"]
        self.inbox_dir = self.root / v["inbox_dir"]
        self._lock = threading.Lock()

    def log(self, heard: str, tier: str, context: str, reply: str, output_note: str | None = None) -> None:
        now = datetime.now()
        path = self.log_dir / f"{now:%Y-%m-%d}.md"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(
                    f"---\ntype: jarvis-log\ndate: {now:%Y-%m-%d}\ntags: [jarvis]\n---\n\n"
                    f"# Jarvis — {now:%Y-%m-%d}\n"
                )
            entry = f"\n## {now:%H:%M:%S} · {tier} · {context}\n> {heard}\n\n{reply.strip()}\n"
            if output_note:
                entry += f"\nFull output: [[{output_note}]]\n"
            with path.open("a") as f:
                f.write(entry)

    def save_output(self, heard: str, context: str, result: str, session_id: str | None) -> str:
        now = datetime.now()
        rel = f"{self.output_dir.relative_to(self.root)}/{now:%Y-%m-%d-%H%M%S}"
        path = self.root / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: jarvis-output\ndate: {now:%Y-%m-%d}\ncontext: {context}\n"
            f"session: {session_id or ''}\ntags: [jarvis]\n---\n\n"
            f"# {heard[:80]}\n\n> {heard}\n\n{result.strip()}\n"
        )
        return rel

    def add_inbox(self, text: str) -> Path:
        now = datetime.now()
        path = self.inbox_dir / f"{now:%Y-%m-%d-%H%M%S}-voice.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: inbox\nsource: jarvis-voice\nstatus: unprocessed\n"
            f"created: {now:%Y-%m-%dT%H:%M:%S}\n---\n\n{text.strip()}\n"
        )
        return path
