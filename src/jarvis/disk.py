"""Disk housekeeping: delete clean, merged git worktrees; move files to the external SSD.

Nothing here runs from a model's tool call. Jarvis builds a plan, reads it aloud, asks for
confirmation, and executes only after the user says "confirm" (see Brain). Execution re-checks
every item, never uses `git worktree remove --force`, and deletes a moved source only after
every copied file is verified on the SSD.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jarvis.disk")

WORKTREE_RE = re.compile(r"\b(delete|remove|clean(\s*up)?|prune|clear)\b.*\bwork\s*trees?\b", re.I)
MOVE_RE = re.compile(
    r"\b(move|offload|transfer|archive|shift)\b.*\b(ssd|external|sushicodee drive|hard drive)\b", re.I
)
CONFIRM_RE = re.compile(r"\bconfirm(ed|s)?\b", re.I)
NEGATION_RE = re.compile(r"\b(don'?t|do not|not|no|cancel|stop|wait)\b", re.I)


class DiskError(Exception):
    """A plan cannot be built; the message is spoken to the user."""


def intent(text: str) -> str | None:
    if WORKTREE_RE.search(text):
        return "worktrees"
    if MOVE_RE.search(text):
        return "move"
    return None


def is_confirm(text: str) -> bool:
    return bool(CONFIRM_RE.search(text)) and not NEGATION_RE.search(text)


@dataclass
class Plan:
    kind: str  # worktrees | move
    items: list[dict]
    summary: str  # spoken
    detail: str  # markdown, saved to the vault
    created: float = field(default_factory=time.time)


def human(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} gigabytes"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} megabytes"
    return f"{max(n, 0) / 1024:.0f} kilobytes" if n >= 1024 else "under a kilobyte"


def _git(args: list[str], cwd: Path | str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _du_bytes(path: Path) -> int:
    try:
        p = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=300)
        return int(p.stdout.split()[0]) * 1024
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        return 0


def _symlink_only_untracked(wt: Path, porcelain: str) -> tuple[bool, list[Path]]:
    """Clean = no tracked changes and no untracked files except symlinks (e.g. a node_modules link)."""
    links: list[Path] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            p = wt / line[3:].strip().strip('"').rstrip("/")
            if p.is_symlink():
                links.append(p)
                continue
        return False, []
    return True, links


class Disk:
    def __init__(self, cfg: dict):
        d = cfg["disk"]
        self.roots = [Path(os.path.expanduser(r)) for r in d["worktree_roots"]]
        self.base_refs: list[str] = d["merged_base_refs"]
        self.check_prs: bool = d.get("check_merged_prs", True)
        self.ssd = Path(d["ssd_mount"])
        self.dest = Path(d["ssd_dest"])
        self.home = Path.home().resolve()
        self.protected = [Path(os.path.expanduser(p)).resolve() for p in d["protected"]]
        self._ref_cache: dict[str, list[str]] = {}

    # ================= worktrees =================
    def _main_repos(self):
        for root in self.roots:
            if not root.is_dir():
                continue
            for c in sorted(root.iterdir()):
                if (c / ".git").is_dir():
                    yield c

    @staticmethod
    def _list_worktrees(repo: Path) -> list[dict]:
        rc, out, _ = _git(["worktree", "list", "--porcelain"], repo)
        if rc:
            return []
        blocks, cur = [], {}
        for line in out.splitlines() + [""]:
            if not line:
                if cur:
                    blocks.append(cur)
                cur = {}
                continue
            key, _, val = line.partition(" ")
            cur[key] = val or True
        return blocks[1:]  # first block is the main checkout

    def _refs(self, repo: Path) -> list[str]:
        if str(repo) not in self._ref_cache:
            rc, out, _ = _git(["for-each-ref", "--format=%(refname)", *self.base_refs], repo)
            self._ref_cache[str(repo)] = out.split() if rc == 0 else []
        return self._ref_cache[str(repo)]

    def _merged(self, repo: Path, head: str, branch: str | None) -> str | None:
        for ref in self._refs(repo):
            rc, _, _ = _git(["merge-base", "--is-ancestor", head, ref], repo)
            if rc == 0:
                return "in " + ref.removeprefix("refs/remotes/")
        if self.check_prs and branch:
            try:
                p = subprocess.run(
                    ["gh", "pr", "list", "--head", branch, "--state", "merged",
                     "--json", "number", "--limit", "1"],
                    cwd=repo, capture_output=True, text=True, timeout=30,
                )
                prs = json.loads(p.stdout or "[]")
                if prs:
                    return f"PR #{prs[0]['number']} merged"
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass
        return None

    def _assess(self, repo: Path, wt: dict) -> dict:
        path = Path(wt["worktree"])
        branch = wt["branch"].removeprefix("refs/heads/") if isinstance(wt.get("branch"), str) else None
        base = {"repo": str(repo), "path": str(path), "branch": branch or "(detached)"}
        if wt.get("prunable") or not path.exists():
            return {**base, "action": "prune", "why": "folder already gone"}
        if wt.get("locked"):
            return {**base, "action": "skip", "why": "locked"}
        rc, out, err = _git(["status", "--porcelain"], path)
        if rc:
            return {**base, "action": "skip", "why": f"git status failed: {err.strip()[:80]}"}
        clean, links = _symlink_only_untracked(path, out)
        if not clean:
            return {**base, "action": "skip", "why": "uncommitted changes"}
        why = self._merged(repo, wt.get("HEAD", "HEAD"), branch)
        if not why:
            return {**base, "action": "skip", "why": "not merged"}
        return {**base, "action": "remove", "why": why, "size": _du_bytes(path),
                "links": [str(p) for p in links]}

    def plan_worktrees(self) -> Plan:
        tasks = [(repo, wt) for repo in self._main_repos() for wt in self._list_worktrees(repo)]
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda a: self._assess(*a), tasks))
        remove = [r for r in results if r["action"] == "remove"]
        prune = [r for r in results if r["action"] == "prune"]
        skipped = [r for r in results if r["action"] == "skip"]
        total = sum(r["size"] for r in remove)
        repos = {Path(r["repo"]).name for r in remove}

        lines = [f"# Worktree cleanup plan\n\nChecked {len(results)} worktrees under "
                 f"{', '.join(map(str, self.roots))}.\n",
                 f"## Delete ({len(remove)}, {human(total)})\n", "| Worktree | Branch | Why safe | Size |",
                 "|---|---|---|---|"]
        lines += [f"| `{r['path']}` | {r['branch']} | {r['why']} | {human(r['size'])} |" for r in remove]
        if prune:
            lines += [f"\n## Prune stale entries ({len(prune)})\n"] + [f"- `{r['path']}`" for r in prune]
        lines += [f"\n## Keep ({len(skipped)})\n"] + [f"- `{r['path']}`: {r['why']}" for r in skipped]

        if not remove and not prune:
            summary = (f"I checked {len(results)} worktrees. None are both clean and merged, "
                       "so there's nothing safe to delete.")
        else:
            summary = (f"I found {len(remove)} worktrees that are clean and merged, about {human(total)}, "
                       f"across {len(repos)} repos.")
            if prune:
                summary += f" Plus {len(prune)} stale entries whose folders are already gone."
            summary += f" {len(skipped)} others have changes or aren't merged, so I'll keep them."
        return Plan("worktrees", remove + prune, summary, "\n".join(lines))

    def run_worktrees(self, plan: Plan) -> tuple[str, str]:
        done, failed, freed, pruned_repos = [], [], 0, set()
        for it in plan.items:
            repo = Path(it["repo"])
            if it["action"] == "prune":
                if it["repo"] not in pruned_repos:
                    _git(["worktree", "prune"], repo)
                    pruned_repos.add(it["repo"])
                continue
            path = Path(it["path"])
            rc, out, err = _git(["status", "--porcelain"], path)
            clean, links = _symlink_only_untracked(path, out) if rc == 0 else (False, [])
            if not clean:
                failed.append((it["path"], "changed since the plan"))
                continue
            for link in links:
                link.unlink()
            rc, _, err = _git(["worktree", "remove", str(path)], repo, timeout=600)
            if rc:
                failed.append((it["path"], err.strip()[:200]))
            else:
                done.append(it["path"])
                freed += it.get("size", 0)
        spoken = f"Done. Deleted {len(done)} worktrees and freed about {human(freed)}."
        if failed:
            spoken += f" {len(failed)} failed; details are in the vault."
        detail = "\n".join([f"# Worktree cleanup result\n\nDeleted {len(done)}, freed {human(freed)}.\n"]
                           + [f"- deleted `{p}`" for p in done]
                           + [f"- FAILED `{p}`: {e}" for p, e in failed])
        return spoken, detail

    # ================= move to SSD =================
    def _reject(self, path: Path) -> str | None:
        if not path.exists() and not path.is_symlink():
            return "doesn't exist"
        if path.is_symlink():
            return "is a symlink"
        real = path.resolve()
        if real == self.home or self.home not in real.parents:
            return "is outside your home folder"
        if real.name.startswith(".") or any(part.startswith(".") for part in real.relative_to(self.home).parts):
            return "is a hidden or config path"
        for p in self.protected:
            if real == p or p in real.parents or real in p.parents:
                return f"is protected ({p})"
        if real.is_dir() and any(Path(r, ".git").exists() for r, _, _ in os.walk(real)):
            return "contains a git repository"
        return None

    @staticmethod
    def _scan(path: Path) -> tuple[int, int, int]:
        """(regular files, symlinks, bytes)."""
        if path.is_file():
            return 1, 0, path.stat().st_size
        files = links = size = 0
        for root, dirs, names in os.walk(path):
            for n in dirs:
                if os.path.islink(os.path.join(root, n)):
                    links += 1
            for n in names:
                p = os.path.join(root, n)
                if os.path.islink(p):
                    links += 1
                elif os.path.isfile(p):
                    files += 1
                    size += os.path.getsize(p)
        return files, links, size

    def _dest_for(self, path: Path) -> Path:
        dest = self.dest / path.resolve().relative_to(self.home)
        if dest.exists():
            dest = dest.with_name(f"{dest.name} (moved {time.strftime('%Y-%m-%d %H%M')})")
        return dest

    def plan_move(self, paths: list[str]) -> Plan:
        if not os.path.ismount(self.ssd):
            raise DiskError(f"The {self.ssd.name} SSD isn't plugged in, so I can't move anything.")
        items, rejected = [], []
        for raw in dict.fromkeys(paths):
            path = Path(os.path.expanduser(raw))
            why = self._reject(path)
            if why:
                rejected.append((raw, why))
                continue
            files, links, size = self._scan(path)
            items.append({"src": str(path.resolve()), "dest": str(self._dest_for(path)),
                          "files": files, "links": links, "size": size})
        total = sum(i["size"] for i in items)
        free = shutil.disk_usage(self.ssd).free
        if total > free * 0.95:
            raise DiskError(f"That's {human(total)} but the SSD only has {human(free)} free.")

        lines = [f"# Move to SSD plan\n\nDestination root `{self.dest}` ({human(free)} free).\n",
                 "| From | To | Files | Size |", "|---|---|---|---|"]
        lines += [f"| `{i['src']}` | `{i['dest']}` | {i['files']} | {human(i['size'])} |" for i in items]
        if rejected:
            lines += ["\n## Refused\n"] + [f"- `{p}`: {w}" for p, w in rejected]

        if not items:
            reasons = "; ".join(f"{Path(p).name} {w}" for p, w in rejected[:3])
            raise DiskError("I couldn't find anything safe to move." + (f" {reasons}." if reasons else ""))
        names = ", ".join(Path(i["src"]).name for i in items[:4]) + (" and more" if len(items) > 4 else "")
        summary = (f"I'll move {names} to the {self.ssd.name} SSD, about {human(total)}. "
                   "Each original is deleted only after every file is copied and checked.")
        links = sum(i["links"] for i in items)
        if links:
            summary += f" {links} shortcut{"s" if links > 1 else ""} (symlinks) can't be stored on that drive and will be dropped."
        if rejected:
            summary += f" I'm skipping {len(rejected)} path{"s" if len(rejected) > 1 else ""} that aren't safe to move."
        return Plan("move", items, summary, "\n".join(lines))

    @staticmethod
    def _copy(src: Path, dest: Path) -> None:
        def copy_file(s: str, d: str) -> None:
            shutil.copyfile(s, d)
            st = os.stat(s)
            try:
                os.utime(d, (st.st_atime, st.st_mtime))
            except OSError:
                pass

        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_file(str(src), str(dest))
            return
        for root, dirs, names in os.walk(src):
            rel = Path(root).relative_to(src)
            (dest / rel).mkdir(parents=True, exist_ok=True)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for n in names:
                s = os.path.join(root, n)
                if os.path.isfile(s) and not os.path.islink(s):
                    copy_file(s, str(dest / rel / n))

    @staticmethod
    def _verify(src: Path, dest: Path) -> list[str]:
        if src.is_file():
            ok = dest.is_file() and dest.stat().st_size == src.stat().st_size
            return [] if ok else [str(src)]
        bad = []
        for root, dirs, names in os.walk(src):
            rel = Path(root).relative_to(src)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for n in names:
                s = os.path.join(root, n)
                if os.path.islink(s) or not os.path.isfile(s):
                    continue
                d = dest / rel / n
                if not d.is_file() or d.stat().st_size != os.path.getsize(s):
                    bad.append(s)
        return bad

    def run_move(self, plan: Plan) -> tuple[str, str]:
        if not os.path.ismount(self.ssd):
            return f"The {self.ssd.name} SSD was unplugged. Nothing was moved.", "SSD not mounted at run time."
        moved, kept, out = 0, 0, ["# Move to SSD result\n"]
        for it in plan.items:
            src, dest = Path(it["src"]), Path(it["dest"])
            if self._reject(src):
                kept += 1
                out.append(f"- SKIPPED `{src}`: no longer safe to move")
                continue
            try:
                self._copy(src, dest)
                bad = self._verify(src, dest)
            except OSError as e:
                kept += 1
                out.append(f"- FAILED `{src}`: copy error {e}; original kept")
                continue
            if bad:
                kept += 1
                out.append(f"- FAILED `{src}`: {len(bad)} files did not verify; original kept")
                continue
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
            moved += 1
            out.append(f"- moved `{src}` -> `{dest}` ({it['files']} files, {human(it['size'])})")
        spoken = f"Done. Moved {moved} of {len(plan.items)} to the SSD."
        if kept:
            spoken += f" {kept} stayed on the Mac because the copy didn't check out; details in the vault."
        return spoken, "\n".join(out)
