# jarvis-agent

Local voice front-end for Claude Code.

```
hotkey (Raycast) -> POST /toggle -> mic -> mlx-whisper (large-v3-turbo, local)
  -> router: Jev (OpenRouter) or keyword fallback
     t1: shell command from config.yaml        (what time, music, notes...)
     t2: quick answer (jev-router, or Claude Haiku via `claude -p`)
     t3: `claude -p --resume <session>` in a per-context working dir (vault, personal, or your own work contexts)
  -> Kokoro-82M TTS (local; `say` fallback)
  -> Obsidian: jarvis/YYYY-MM-DD.md log, outputs/jarvis/*.md, inbox/*.md
```

## Config
`config.yaml` holds public defaults. Put your real setup (work contexts, paths, MCP servers, routing
hints) in `config.local.yaml`, which is gitignored and merged over it (dicts merge, lists and values
replace; set a key to `null` to drop a default).

## Run
```bash
uv sync
uv run jarvis setup-tts                 # once: Kokoro model (~350 MB)
pm2 start ecosystem.config.cjs          # jarvis daemon + nightly jarvis-inbox (23:30)
```

Hotkeys via skhd (`~/.skhdrc`, needs Accessibility): ctrl+alt+J talk/stop (auto-stops after 1.2 s
silence), ctrl+alt+K cancel / shut up. Raycast script commands in `raycast/` are an alternative.

## HUD
`hud/` (Electron, pm2 app `jarvis-hud`): floating arc-reactor orb, always on top, all Spaces,
click-through except orb/bubble. Click = talk/stop, drag = move (position saved to
`~/.cache/jarvis/hud.json`), right-click = menu. Colour: cyan idle/listening, amber transcribing,
purple thinking/working, green speaking, grey = daemon down. Setup: `cd hud && npm install`.
pm2 must not inherit `ELECTRON_RUN_AS_NODE` (ecosystem uses `env -u`). pm2 runs `.venv/bin/jarvis`
directly: under `uv run` a restart orphaned the Python child, which kept port 8765.

## Disk housekeeping (`src/jarvis/disk.py`)
Say "delete my merged worktrees" or "move <folder> to the SSD". Jarvis builds a plan, saves it to
`outputs/jarvis/`, reads the summary aloud and asks "Should I go ahead? Say confirm to proceed, or
cancel." The mic opens for 8 s; the plan stays answerable for 120 s (hotkey, then "confirm"). Only a
reply containing "confirm" without a negation runs it; anything else cancels. HUD turns red while waiting.

- Worktrees (under `disk.worktree_roots`): deleted only if `git status` is clean (untracked symlinks
  allowed) and HEAD is in `origin/PRODUCTION*`/`main`/`master` or the branch has a merged PR.
  `git worktree remove` without `--force`; branches are kept. Status is re-checked at run time.
- Moves: destination `/Volumes/Sushicodee/MacOffload/<path under ~>` (exFAT: symlinks dropped).
  Refuses hidden paths, `disk.protected`, anything containing a git repo, anything outside `~`.
  Copy, verify every file's size on the SSD, then delete the original. Any mismatch keeps the original.
- Paths for a move come from a read-only Claude resolver (`Glob`, `ls`, `du`); it cannot move anything.

## Profile, data access, blocked runs, backups
- `vault/jarvis/profile.md`: dated facts about you, appended to tier 2 and tier 3 prompts. Edit it directly.
- MCP servers: `tier3.base_mcp_servers` plus each context's `mcp_servers`, resolved by name from your
  Claude Code config (`~/.claude.json`, `~/.claude/settings.json`) at run time and written 0600 to
  `~/.cache/jarvis/mcp-*.json`, so no server definitions or keys live in this repo.
- `claude -p` reports success even when a permission denial stopped the work. Jarvis reads
  `permission_denials` and says "Heads up, I was blocked from using <tool>" first; the saved output
  lists each blocked call.
- `scripts/backup-vault.sh` (pm2 `jarvis-vault-backup`, 23:45 daily): verified tar.gz snapshot of the
  vault to `/Volumes/Sushicodee/Backups/obsidian-vault/`, keeps the newest 30, skips if the SSD is unplugged.

## Skills and subagents
Tier 3 loads your skill and agent folders as local plugins (`tier3.skill_plugins`, default
`user` = `~/.claude`). Only `skills/` and `agents/` are symlinked under
`~/.cache/jarvis/plugins/`, never hooks or settings, so the isolation from `--setting-sources ""` holds.
Every context allows the `Skill` and `Agent` tools. The system prompt tells Claude to pick the best
skill first, fan independent parts out to subagents (domain agents when they match), and offer a
todo when the skill needs tools this session denies. `tier3.skills_hint` adds your own routing rules. Subagents inherit the same allow/deny rules.

## CLI
| Command | Does |
|---|---|
| `jarvis toggle` / `cancel` / `status` | control the daemon |
| `jarvis send "text"` | text command to daemon (skip STT) |
| `jarvis text "text" [--quiet] [--route-only]` | run locally, wait for result |
| `jarvis file a.wav` | transcribe a file and run it |
| `jarvis bench a.wav` | compare STT backends |
| `jarvis inbox` | file `inbox/` notes into the vault via Claude Code |

## Jev
Jev routing needs `OPENROUTER_API_KEY` in `../../mcp-servers/jev-mcp/.env` (same file the jev MCP
reads). Without it, keyword routing (`match:` phrases per command, `pattern:` per context) is used.

## Safety
Tier 3 runs with `--permission-mode default`, `--setting-sources ""` and a per-context
`allowed_tools` list: anything not listed is denied (headless can't prompt). Settings files are NOT
loaded on purpose: the repo's `.claude/settings.local.json` allows `Bash(bash -c *)`, which made
tier 3 effectively unrestricted before 2026-11-18. MCP servers come from `mcp.json`;
`tier3.deny_tools` repeats the `.env` read denies and blocks `rm`, `mv`, `git worktree remove`. Vault context may edit only inside the vault; other
contexts are read-only by default. Widen deliberately in `config.yaml`.

## Measured (M2, 16 GB)
mlx large-v3-turbo 1.0 s for a 2.7 s clip; mlx small.en 0.26 s; openai-whisper CLI small.en 8.5 s.
Tier 1 ~0.3 s, tier 2 (Haiku via claude) ~4.6 s, tier 3 simple vault question ~12 s.
