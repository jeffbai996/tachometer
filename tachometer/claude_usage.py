#!/usr/bin/env python3
"""Token usage and a dollar estimate, read from Claude Code's own transcripts
(<config_dir>/projects/**/*.jsonl).

The $ figure mirrors how Claude Code's /usage computes it: a LOCAL estimate of
tokens x per-model rates. On a subscription it is NOT a bill — it is what the
same work would have cost through the API, which is the number that says
whether a workflow was worth its tokens.

Rates and the cache multipliers come from the user's config file, never from
this module — published prices go stale, and a tier this file has never heard
of should be addable without a code change. See config.py.
"""
from __future__ import annotations
import json
import sys
import glob
import os
from typing import Any

from . import config

def rate_for(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens for a model id, or None.

    Matched by SUBSTRING against the configured keys, because model ids carry
    version and date suffixes that the rate does not depend on:
    "claude-opus-4-8" and "claude-opus-5" both price off "opus".

    LONGEST KEY FIRST. With plain dict order a key like "opus" would win over a
    more specific "opus-4-8" purely because it was written earlier in the file,
    which makes a user's careful override silently ineffective. Specificity
    should decide, not authoring order.

    Returning None rather than a default is deliberate: an unknown model is
    counted as $0, and `--doctor` names it. A guessed default would bury the
    same problem inside a plausible-looking total.
    """
    m = (model or "").lower()
    rates = config.load()["rates"]
    for key in sorted(rates, key=len, reverse=True):
        if key.lower() in m:
            pair = rates[key]
            return (float(pair[0]), float(pair[1]))
    return None


# Kept as a private alias so the rest of this module reads unchanged.
_rate_for = rate_for


def _cost(model: str, inp: int, out: int, cread: int, cwrite: int) -> float:
    rate = _rate_for(model)
    if not rate:
        return 0.0
    cfg = config.load()
    in_rate, out_rate = rate
    return (
        inp * in_rate
        + out * out_rate
        + cread * in_rate * cfg["cache_read_multiplier"]
        + cwrite * in_rate * cfg["cache_write_multiplier"]
    ) / 1_000_000


def compute(projects_dir: str) -> dict[str, Any]:
    """Aggregate usage across every transcript under projects_dir."""
    by_model: dict[str, dict[str, float]] = {}
    turns = 0
    files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") if isinstance(obj, dict) else None
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    model = msg.get("model") or "unknown"
                    if model == "<synthetic>":
                        continue
                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                    cread = int(usage.get("cache_read_input_tokens") or 0)
                    cwrite = int(usage.get("cache_creation_input_tokens") or 0)
                    if inp == out == cread == cwrite == 0:
                        continue
                    turns += 1
                    slot = by_model.setdefault(model, {
                        "input": 0, "output": 0, "cache_read": 0,
                        "cache_write": 0, "cost": 0.0, "turns": 0,
                    })
                    slot["input"] += inp
                    slot["output"] += out
                    slot["cache_read"] += cread
                    slot["cache_write"] += cwrite
                    slot["cost"] += _cost(model, inp, out, cread, cwrite)
                    slot["turns"] += 1
        except (OSError, IOError):
            continue

    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    for slot in by_model.values():
        for k in ("input", "output", "cache_read", "cache_write", "cost"):
            total[k] += slot[k]
    return {"by_model": by_model, "total": total, "turns": turns, "files": len(files)}


def _fmt(n: float) -> str:
    return f"{int(n):,}"


def main() -> None:
    projects = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"), "projects"))
    res = compute(projects)
    if "--json" in sys.argv:
        print(json.dumps(res))
        return
    t = res["total"]
    print(f"transcripts: {res['files']} files, {res['turns']} turns")
    print(f"input {_fmt(t['input'])} · output {_fmt(t['output'])} · "
          f"cache_read {_fmt(t['cache_read'])} · cache_write {_fmt(t['cache_write'])}")
    print(f"EST COST: ${t['cost']:,.2f}")
    print("--- by model ---")
    for model, s in sorted(res["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        print(f"  {model:28} ${s['cost']:8,.2f}  "
              f"(in {_fmt(s['input'])} out {_fmt(s['output'])} "
              f"cr {_fmt(s['cache_read'])} cw {_fmt(s['cache_write'])}, {s['turns']} turns)")


if __name__ == "__main__":
    main()
