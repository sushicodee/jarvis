"""Config loading: config.yaml (public defaults) with config.local.yaml (private, gitignored) merged
over it. Paths are expanded relative to the agent root."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

AGENT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.path.expanduser("~/.cache/jarvis"))


def _expand(p: str) -> Path:
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else (AGENT_ROOT / path).resolve()


def _merge(base: dict, over: dict) -> dict:
    """Recursive dict merge; anything that isn't a dict on both sides (lists, scalars) is replaced."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config() -> dict:
    cfg_path = Path(os.environ.get("JARVIS_CONFIG", AGENT_ROOT / "config.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text())
    local = cfg_path.with_name("config.local.yaml")
    if local.exists():
        cfg = _merge(cfg, yaml.safe_load(local.read_text()) or {})
    cfg["vault"]["path"] = _expand(cfg["vault"]["path"])
    cfg["jev"]["env_file"] = _expand(cfg["jev"]["env_file"])
    cfg["tts"]["model_dir"] = _expand(cfg["tts"]["model_dir"])
    for ctx in cfg["tier3"]["contexts"].values():
        ctx["cwd"] = _expand(ctx["cwd"])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return cfg


def load_env_file(path: Path) -> None:
    """KEY=VALUE loader; existing env wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
