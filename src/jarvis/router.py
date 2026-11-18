"""Jev 3-tier router: one /v1/systemone batch call picks tier, tier-1 command, and tier-3 context."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass

import httpx

from .config import STATE_DIR, load_env_file

QUESTION_WORDS = {"what", "what's", "whats", "who", "who's", "when", "where", "why", "how", "is", "are",
                  "does", "do", "can", "define", "explain", "tell"}

log = logging.getLogger("jarvis.router")

TIERS = {
    "t1": "a quick fixed device action or utility that matches one of the listed commands "
          "(open an app, time, date, music, volume, lock screen, take a quick note, reset, stop talking)",
    "t2": "a short general-knowledge question, definition, or chit-chat answerable in a sentence or two "
          "without the user's files, notes, tickets, code, or the internet",
    "t3": "real work: needs the user's notes, files, code, repos, tickets, todos, web research, "
          "writing or editing documents, planning, or multi-step reasoning",
}


@dataclass
class Route:
    tier: str
    command: str | None
    context: str
    confidence: float
    raw: dict | None = None


class Router:
    def __init__(self, cfg: dict):
        j = cfg["jev"]
        load_env_file(j["env_file"])
        self.base = j["base"]
        self.model = j["model"]
        self.min_conf = j["min_confidence"]
        self.commands = {k: v["description"] for k, v in cfg["commands"].items()}
        self.contexts = {k: v["description"] for k, v in cfg["tier3"]["contexts"].items()}
        self.default_context = cfg["tier3"]["default_context"]
        self.matches = {k: [m.lower() for m in v.get("match", [])] for k, v in cfg["commands"].items()}
        self.context_patterns = {k: re.compile(v["pattern"], re.I)
                                 for k, v in cfg["tier3"]["contexts"].items() if v.get("pattern")}
        self.claude_bin = cfg["tier3"]["claude_bin"]
        key = os.environ.get("OPENROUTER_API_KEY", "")
        self.has_key = bool(key)
        if not key:
            log.warning("OPENROUTER_API_KEY not set (%s); keyword routing + Claude Haiku for tier 2", j["env_file"])
        self.client = httpx.Client(
            base_url=self.base,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "X-Title": "jarvis-agent"},
            timeout=20.0,
        )

    def _systemone(self, state: str, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(3):
            r = self.client.post("/v1/systemone", json=body)
            if r.status_code in (429, 529) and attempt < 2:
                time.sleep(2**attempt + random.random())
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("jev rate limited")

    def route(self, text: str) -> Route:
        questions = {
            "tier": {"type": "choice",
                     "instructions": "Which kind of request is this spoken command to a voice assistant?",
                     "criteria": TIERS},
            "command": {"type": "choice",
                        "instructions": "Which fixed command does the spoken request ask for?",
                        "criteria": {**self.commands, "none": "none of the listed commands fits"}},
            "context": {"type": "choice",
                        "instructions": "Which workspace does this request belong to?",
                        "criteria": self.contexts},
        }
        kw = self.keyword_route(text)
        if not self.has_key or kw.tier == "t1":  # exact phrase hit is certain and free
            return kw
        try:
            res = self._systemone(f"Spoken request: {text!r}", questions)["answers"]
        except Exception as e:  # noqa: BLE001
            log.warning("jev failed (%s); using keyword router", e)
            return self.keyword_route(text)

        tier, tconf = res["tier"]["choice"], float(res["tier"].get("confidence", 0))
        cmd, cconf = res["command"]["choice"], float(res["command"].get("confidence", 0))
        ctx, xconf = res["context"]["choice"], float(res["context"].get("confidence", 0))
        if xconf < self.min_conf:
            ctx = self.default_context

        # A confident command match wins even if the tier call wavered.
        if cmd != "none" and cconf >= self.min_conf and tier in ("t1", "t2"):
            return Route("t1", cmd, ctx, cconf, res)
        if tier == "t1" and (cmd == "none" or cconf < self.min_conf):
            tier = "t2"
        if tconf < self.min_conf:
            tier = "t3"
        return Route(tier, None, ctx, tconf, res)

    def keyword_route(self, text: str) -> Route:
        """No-key fallback: phrase match for tier 1, length/possessive heuristic for tier 2 vs 3."""
        low = re.sub(r"[^\w\s']", " ", text.lower())
        low = f" {' '.join(low.split())} "
        ctx = next((k for k, p in self.context_patterns.items() if p.search(text)), self.default_context)
        for cmd, phrases in self.matches.items():
            if any(f" {p} " in low for p in phrases):
                return Route("t1", cmd, ctx, 1.0)
        words = low.split()
        personal = re.search(r"\b(my|our|me|i|notes?|vault|ticket|pr|jira|repo|code|file|todo|write|draft|plan)\b", low)
        if len(words) <= 12 and words and words[0] in QUESTION_WORDS and not personal:
            return Route("t2", None, ctx, 0.6)
        return Route("t3", None, ctx, 0.6)

    def quick_answer(self, text: str, model: str, system: str, max_tokens: int) -> str:
        if not self.has_key:
            return self._haiku_via_claude(text, system)
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        r = self.client.post("/v1/chat/completions", json=body, timeout=60.0)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""

    def _haiku_via_claude(self, text: str, system: str) -> str:
        """Tier 2 without OpenRouter: Claude Code subscription auth, no tools/MCP/settings/hooks."""
        cmd = [self.claude_bin, "-p", "--model", "haiku", "--output-format", "json",
               "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--system-prompt", system]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=60,
                              cwd=STATE_DIR, env=env)
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(data.get("result", "claude error"))
        return data.get("result", "")
