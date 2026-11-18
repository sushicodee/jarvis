"""Dispatch a transcript through Jev routing to tier 1 (shell), tier 2 (quick model), tier 3 (Claude Code)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AGENT_ROOT, STATE_DIR
from .disk import Disk, DiskError, Plan, intent as disk_intent, is_confirm
from .router import Route, Router
from .tts import TTS
from .vault import Vault

log = logging.getLogger("jarvis.brain")

SPOKEN_INSTR = (
    "This request arrived by voice through Jarvis, a local voice assistant; the user is listening, "
    "not reading; address them as Sushicodee. "
    "Do the work normally. End your reply with one final line, exactly: "
    "`SPOKEN: <one or two plain sentences, no markdown, summarizing the outcome for text-to-speech>`."
)

INBOX_PROMPT = (
    "Process every note in inbox/ whose frontmatter has status: unprocessed, following the filing "
    "rules in CLAUDE.md. For each: write or extend the right note under wiki/ (or the matching "
    "existing folder), add wikilinks, then set the inbox note's status to processed and add a "
    "`filed_to:` frontmatter field. Never delete notes. Finish with a short summary of what was filed."
)

RESOLVE_PROMPT = (
    "The user said, by voice: \"{text}\"\n"
    "Work out which files or folders on this Mac they want moved to their external SSD. "
    "Home is {home}. Explore with ls, du and Glob only. Do not move, copy or delete anything. "
    "If the request is ambiguous, choose nothing. "
    "End with exactly one line: PATHS: [\"/absolute/path\", ...] (an empty list if unsure)."
)
SKILLS_INSTR = (
    "\n\nSkills and subagents: you have the user's own skills and agents (namespaced by the plugin "
    "they come from). Before doing the work yourself, decide which skill fits best and invoke it with "
    "the Skill tool. Split independent parts across subagents with the Agent tool, in parallel, "
    "preferring a domain-specific agent when one matches and Explore for broad code searches. "
    "If the right skill needs tools this session denies (file edits outside the allowed paths, shell, "
    "ticket/chat/email writes, PRs), do not look for workarounds: say which skill would do it and "
    "offer to queue it as a todo (mcp__todo__add_todo) for the user to run. Say which skill or agents "
    "you used in the SPOKEN line."
)
ASK = " Should I go ahead? Say confirm to proceed, or cancel."

SESSIONS_FILE = STATE_DIR / "sessions.json"


@dataclass
class Result:
    route: Route
    reply: str
    pending: bool = False  # tier 3 still running in background


class Brain:
    def __init__(self, cfg: dict, tts: TTS | None, on_ask=None):
        self.cfg = cfg
        self.router = Router(cfg)
        self.tts = tts
        self.vault = Vault(cfg)
        self._sessions_lock = threading.Lock()
        self.jobs: dict[int, str] = {}
        self.last: dict = {}
        self._job_seq = 0
        self.disk = Disk(cfg)
        self.pending: Plan | None = None
        self.on_ask = on_ask  # opens the mic after Jarvis asks a question

    # ---------- public ----------
    def handle(self, text: str, wait: bool = False) -> Result:
        text = re.sub(r"^\s*(hey|ok|okay)?[\s,]*jarvis[\s,.!?]*", "", text, flags=re.I).strip() or text
        plan, self.pending = self.pending, None
        if plan and time.time() - plan.created < self.cfg["disk"]["confirm_ttl_seconds"]:
            return self._answer(text, plan)
        kind = disk_intent(text)
        if kind:
            return self._disk_request(kind, text)
        route = self.router.route(text)
        log.info("heard=%r route=%s cmd=%s ctx=%s conf=%.2f",
                 text, route.tier, route.command, route.context, route.confidence)
        if route.tier == "t1":
            reply = self._tier1(route.command, text)
            self._finish(text, route, reply)
            return Result(route, reply)
        if route.tier == "t2":
            t = self.cfg["tier2"]
            try:
                reply = self.router.quick_answer(text, t["model"], t["system"] + self.profile_block(), t["max_tokens"])
            except Exception as e:  # noqa: BLE001
                log.warning("tier2 failed (%s); escalating to tier 3", e)
                route.tier = "t3"
            else:
                self._finish(text, route, reply)
                return Result(route, reply)
        return self._tier3(text, route, wait)

    def _record(self, heard: str, tier: str, ctx: str, reply: str) -> None:
        self.last = {"heard": heard, "tier": tier, "context": ctx, "reply": reply, "ts": time.time()}

    def speak(self, text: str) -> None:
        if self.tts and text:
            self.tts.speak(text)

    # ---------- tier 1 ----------
    def _tier1(self, cmd: str, text: str) -> str:
        if cmd == "take_note":
            path = self.vault.add_inbox(text)
            return f"Noted. Saved to inbox as {path.stem}."
        if cmd == "reset_session":
            with self._sessions_lock:
                SESSIONS_FILE.write_text("{}")
            return "Fresh start. Sessions cleared."
        if cmd == "stop_speaking":
            if self.tts:
                self.tts.stop()
            return ""
        spec = self.cfg["commands"][cmd]
        proc = subprocess.run(spec["run"], shell=True, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            log.warning("command %s failed: %s", cmd, proc.stderr.strip())
            return f"That command failed: {cmd.replace('_', ' ')}."
        if spec.get("speak_output"):
            return proc.stdout.strip()
        return spec.get("say", "Done.")

    def profile_block(self) -> str:
        """Typed facts about the user (vault jarvis/profile.md), appended to model system prompts."""
        path = self.vault.root / self.cfg["vault"]["profile"]
        try:
            text = path.read_text()[:4000]
        except OSError:
            return ""
        return f"\n\nFacts about the user (dated; prefer newer, never repeat aloud unprompted):\n{text}"

    # ---------- disk (plan -> ask -> confirm -> run) ----------
    def cancel_pending(self) -> None:
        self.pending = None

    def _bg(self, label: str, fn) -> None:
        self._job_seq += 1
        job_id = self._job_seq
        self.jobs[job_id] = label

        def worker() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("job %s failed", label)
                self.speak("That job failed. Check the Jarvis log.")
            finally:
                self.jobs.pop(job_id, None)

        threading.Thread(target=worker, daemon=True, name=f"job-{job_id}").start()

    def _disk_request(self, kind: str, text: str) -> Result:
        route = Route("disk", kind, "disk", 1.0)

        def work() -> None:
            try:
                plan = self.disk.plan_worktrees() if kind == "worktrees" else self.disk.plan_move(self._resolve(text))
            except DiskError as e:
                self.vault.log(text, "disk", kind, str(e))
                self._record(text, "disk", kind, str(e))
                self.speak(str(e))
                return
            note = self.vault.save_output(text, "disk", plan.detail, None)
            if not plan.items:
                self.vault.log(text, "disk", kind, plan.summary, note)
                self._record(text, "disk", kind, plan.summary)
                self.speak(plan.summary)
                return
            plan.created = time.time()
            self.pending = plan
            question = plan.summary + ASK
            self.vault.log(text, "disk", kind, question, note)
            self._record(text, "disk", kind, question)
            self.speak(question)
            if self.on_ask and self.pending is plan:
                self.on_ask()

        ack = "Checking your worktrees. This takes about three minutes." if kind == "worktrees" else "Working out what to move."
        self._record(text, "disk", kind, ack)
        self._bg(text, work)
        return Result(route, ack, pending=True)

    def _resolve(self, text: str) -> list[str]:
        home = str(Path.home())
        ctx = {"cwd": home, "model": self.cfg["disk"]["resolver_model"],
               "allowed_tools": ["Glob", "Bash(ls:*)", "Bash(du:*)"]}
        result, _, is_error, _ = self.run_claude(RESOLVE_PROMPT.format(text=text, home=home), ctx, None,
                                              "Unattended resolver. No questions.")
        m = re.search(r"^PATHS:\s*(\[.*\])\s*$", result or "", re.M)
        if is_error or not m:
            raise DiskError("I couldn't work out which files you meant. Try naming the folder.")
        try:
            paths = [p for p in json.loads(m.group(1)) if isinstance(p, str)]
        except json.JSONDecodeError:
            paths = []
        if not paths:
            raise DiskError("I couldn't work out which files you meant. Try naming the folder.")
        return paths

    def _answer(self, text: str, plan: Plan) -> Result:
        route = Route("disk", "confirm", "disk", 1.0)
        if not is_confirm(text):
            reply = "Cancelled. Nothing was changed."
            self._finish(text, route, reply)
            return Result(route, reply)

        def work() -> None:
            run = self.disk.run_worktrees if plan.kind == "worktrees" else self.disk.run_move
            spoken, detail = run(plan)
            note = self.vault.save_output(f"{plan.kind} (confirmed)", "disk", detail, None)
            self.vault.log(text, "disk", plan.kind, spoken, note)
            self._record(text, "disk", plan.kind, spoken)
            self.speak(spoken)

        ack = ("Confirmed. Deleting the worktrees now." if plan.kind == "worktrees"
               else "Confirmed. Moving to the SSD now. Big folders take a while.")
        self.vault.log(text, "disk", plan.kind, ack)
        self._record(text, "disk", plan.kind, ack)
        self._bg(f"{plan.kind}: running", work)
        return Result(route, ack, pending=True)

    # ---------- tier 3 ----------
    def _tier3(self, text: str, route: Route, wait: bool) -> Result:
        if wait:
            reply = self._run_tier3(text, route)
            return Result(route, reply)
        self._job_seq += 1
        job_id = self._job_seq
        self.jobs[job_id] = text
        ack = f"On it, in {route.context}."
        self._record(text, "t3", route.context, ack)

        def worker() -> None:
            try:
                self._run_tier3(text, route)
            finally:
                self.jobs.pop(job_id, None)

        threading.Thread(target=worker, daemon=True, name=f"t3-{job_id}").start()
        return Result(route, ack, pending=True)

    def _run_tier3(self, text: str, route: Route) -> str:
        ctx = self.cfg["tier3"]["contexts"][route.context]
        sid = self._get_session(route.context)
        hint = self.cfg["tier3"].get("skills_hint") or ""
        system = SPOKEN_INSTR + SKILLS_INSTR + (f" {hint}" if hint else "") + self.profile_block()
        result, new_sid, is_error, denials = self.run_claude(text, ctx, sid, system)
        if is_error and sid:
            log.info("resume failed for %s; retrying fresh", route.context)
            result, new_sid, is_error, denials = self.run_claude(text, ctx, None, system)
        if new_sid and not is_error:
            self._set_session(route.context, new_sid)

        body, spoken = split_spoken(result)
        if is_error:
            spoken = "Claude hit an error. Details are in the vault log."
        if denials:
            names = ", ".join(dict.fromkeys(d.split(":")[0] for d in denials))
            spoken = f"Heads up, I was blocked from using {names}, so this may be incomplete. {spoken}"
            body = (body or result) + "\n\n## Blocked tool calls\n" + "\n".join(f"- `{d}`" for d in denials)
        note = self.vault.save_output(text, route.context, body or result, new_sid)
        self.vault.log(text, "t3", route.context, spoken, note)
        self._record(text, "t3", route.context, spoken)
        self.speak(spoken)
        return spoken

    def run_claude(self, prompt: str, ctx: dict, sid: str | None, system: str) -> tuple[str, str | None, bool, list[str]]:
        t = self.cfg["tier3"]
        cmd = [t["claude_bin"], "-p", "--output-format", "json",
               "--permission-mode", t["permission_mode"],
               # Load no settings files: the user's/repo's allow rules (e.g. Bash(bash -c *))
               # would otherwise widen allowed_tools. Only the per-context list applies.
               "--setting-sources", "",
               "--disallowedTools", *t["deny_tools"],
               "--mcp-config", str(mcp_config([*t.get("base_mcp_servers", []), *ctx.get("mcp_servers", [])],
                                              t.get("mcp_project_dirs", []))),
               *[arg for d in skill_plugins(t.get("skill_plugins", {})) for arg in ("--plugin-dir", str(d))],
               "--append-system-prompt", system]
        if sid:
            cmd += ["--resume", sid]
        if ctx.get("model"):
            cmd += ["--model", ctx["model"]]
        if ctx.get("allowed_tools"):
            cmd += ["--allowedTools", *ctx["allowed_tools"]]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            proc = subprocess.run(cmd, input=prompt, cwd=ctx["cwd"], capture_output=True,
                                  text=True, timeout=t["timeout_seconds"], env=env)
        except subprocess.TimeoutExpired:
            return "Timed out.", None, True, []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            log.warning("claude non-json output rc=%s stderr=%s", proc.returncode, proc.stderr[-500:])
            return (proc.stdout or proc.stderr).strip(), None, True, []
        # A permission denial still ends as subtype=success / is_error=false, so surface it.
        denials = [describe_denial(d) for d in data.get("permission_denials") or []]
        if denials:
            log.warning("tier3 permission denials: %s", denials)
        return data.get("result", ""), data.get("session_id"), bool(data.get("is_error")), denials

    def process_inbox(self) -> str:
        ctx = self.cfg["tier3"]["contexts"]["vault"]
        result, _, is_error, _ = self.run_claude(INBOX_PROMPT, ctx, None, "Unattended nightly run. No questions.")
        self.vault.log("nightly inbox processing", "t3", "vault", result)
        return result if not is_error else f"inbox run failed: {result}"

    # ---------- helpers ----------
    def _finish(self, text: str, route: Route, reply: str) -> None:
        self.vault.log(text, route.tier, route.command or route.context, reply or "(silent)")
        self._record(text, route.tier, route.command or route.context, reply)
        self.speak(reply)

    def _get_session(self, ctx: str) -> str | None:
        with self._sessions_lock:
            if not SESSIONS_FILE.exists():
                return None
            return json.loads(SESSIONS_FILE.read_text() or "{}").get(ctx)

    def _set_session(self, ctx: str, sid: str) -> None:
        with self._sessions_lock:
            data = json.loads(SESSIONS_FILE.read_text() or "{}") if SESSIONS_FILE.exists() else {}
            data[ctx] = sid
            SESSIONS_FILE.write_text(json.dumps(data, indent=2))


CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _known_mcp_servers(project_dirs: list[str]) -> dict:
    """MCP server definitions the user already configured for Claude Code: ~/.claude/settings.json,
    the given project scopes in ~/.claude.json, then user scope. Read at run time so no server
    definitions or secrets live in this repo."""
    try:
        cj = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        cj = {}
    wanted = {str(Path(os.path.expanduser(d)).resolve()) for d in project_dirs}
    sources = [json.loads(CLAUDE_SETTINGS.read_text()).get("mcpServers", {}) if CLAUDE_SETTINGS.exists() else {}]
    sources += [pv.get("mcpServers") or {} for proj, pv in cj.get("projects", {}).items() if proj in wanted]
    sources.append(cj.get("mcpServers", {}))
    found: dict = {}
    for src in sources:  # later sources win; env/headers merge so one scope's vars aren't dropped
        for name, spec in src.items():
            prev = found.get(name, {})
            merged = {**prev, **spec}
            for key in ("env", "headers"):
                if prev.get(key) or spec.get(key):
                    merged[key] = {**(prev.get(key) or {}), **(spec.get(key) or {})}
            found[name] = merged
    return found


def mcp_config(names: list[str], project_dirs: list[str]) -> Path:
    """Named servers, written 0600 under ~/.cache/jarvis (they can carry API keys)."""
    known = _known_mcp_servers(project_dirs) if names else {}
    servers = {}
    for name in dict.fromkeys(names):
        if name in known:
            servers[name] = known[name]
        else:
            log.warning("mcp server %s not found in Claude config", name)
    out = STATE_DIR / f"mcp-{'-'.join(sorted(servers)) or 'none'}.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": servers}, f)
    return out


def skill_plugins(sources: dict) -> list[Path]:
    """Expose skill/agent folders as local plugins (skills + agents only: no hooks, no settings),
    so tier 3 sees them while --setting-sources "" keeps the user's allow rules and hooks out."""
    dirs = []
    for name, src in sources.items():
        if not src:  # a local config can disable a public default with `name: null`
            continue
        root = STATE_DIR / "plugins" / name
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        manifest = root / ".claude-plugin" / "plugin.json"
        if not manifest.exists():
            manifest.write_text(json.dumps({"name": name, "version": "1.0.0",
                                            "description": f"Jarvis view of {src}"}))
        for kind in ("skills", "agents"):
            target = Path(os.path.expanduser(src)) / kind
            link = root / kind
            if target.is_dir() and not link.exists():
                link.symlink_to(target)
        dirs.append(root)
    return dirs


def describe_denial(d: dict) -> str:
    inp = d.get("tool_input") or {}
    detail = inp.get("command") or inp.get("file_path") or ""
    return f"{d.get('tool_name', '?')}: {detail}"[:200] if detail else d.get("tool_name", "?")


def split_spoken(result: str) -> tuple[str, str]:
    """Return (body without SPOKEN line, spoken summary)."""
    lines = result.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip().strip("`")
        if line.upper().startswith("SPOKEN:"):
            spoken = line.split(":", 1)[1].strip()
            return "\n".join(lines[:i]).rstrip(), spoken
    return result, result[:300]
