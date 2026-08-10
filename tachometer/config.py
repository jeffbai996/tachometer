"""User configuration — where to look, and what tokens cost.

Rates live here rather than in the source for two reasons: published prices
change, and a user on a tier this file has never heard of should be able to
add it without editing code. A model with no rate is counted at $0 and named
by `--doctor`, so a silently-low total is discoverable.
"""
from __future__ import annotations

import os
import tomllib
from typing import Any

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "tachometer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")

# Published list prices, USD per million tokens, [input, output].
# Matched against the model id by substring, longest key first.
DEFAULT_TOML = """\
# Where to look. Claude Code's own config directory; the transcripts are in
# <config_dir>/projects. Overridden by the CLAUDE_CONFIG_DIR environment
# variable if that is set.
config_dir = "~/.claude"

# USD per million tokens, [input, output]. These are published list prices and
# they go stale — edit freely, and add your own tiers. A model with no entry
# here is counted as $0 and reported by `tachometer --doctor`.
[rates]
opus   = [5.0, 25.0]
sonnet = [3.0, 15.0]
haiku  = [1.0, 5.0]

# Cache reads bill at a fraction of the input rate; cache writes at a premium.
cache_read_multiplier  = 0.10
cache_write_multiplier = 1.25
"""

_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    """Read the config, writing the documented default on first run.

    Cached: this is read on every cost computation, and re-parsing a TOML file
    per sample would show up on a 5s poll.
    """
    global _cache
    if _cache is not None:
        return _cache

    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_TOML)

    with open(CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)

    # The environment variable wins: it is what Claude Code itself honours, so
    # a user who has already set it should not have to say so twice.
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        cfg["config_dir"] = env_dir
    cfg.setdefault("config_dir", "~/.claude")
    cfg.setdefault("rates", {})
    cfg.setdefault("cache_read_multiplier", 0.10)
    cfg.setdefault("cache_write_multiplier", 1.25)
    _cache = cfg
    return cfg


def projects_dir() -> str:
    """Absolute path to the transcript directory."""
    return os.path.join(os.path.expanduser(load()["config_dir"]), "projects")
