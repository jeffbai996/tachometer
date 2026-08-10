"""The carve boundary.

This project was extracted from a larger private codebase, and changes are
ported across by hand. The failure mode that costs the most is a port that
quietly drags a dependency or an identity back in — it works on the machine it
came from and breaks, or leaks, everywhere else. These tests are the fence.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "tachometer"
sys.path.insert(0, str(ROOT))

# Modules that only exist in the private codebase this was carved from.
FOREIGN = {"store", "fleet", "status", "bot_config", "inventory", "server",
           "squad_store"}

SOURCES = sorted(PKG.rglob("*.py"))
TEMPLATES = sorted(PKG.rglob("*.html"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_imports_from_the_private_codebase(path: pathlib.Path):
    """A stray `import fleet` runs fine on the machine it was written on and
    is an instant ImportError for everyone else."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — always ours
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    leaked = found & FOREIGN
    assert not leaked, f"{path.name} imports {sorted(leaked)} from the private repo"


@pytest.mark.parametrize("path", SOURCES + TEMPLATES, ids=lambda p: p.name)
def test_no_private_identities_or_hostnames(path: pathlib.Path):
    """Comments carry real design history, which is worth keeping — but not
    the names, hostnames, or tailnet addresses of the deployment it came from.
    Keep the reasoning, drop the identity."""
    text = path.read_text(encoding="utf-8")
    # Patterns, not a list of the actual private names. Spelling them out here
    # would publish in the test file exactly what the test exists to keep out
    # of the shipped source — the check would become the leak.
    # (pattern, flags, why). The attribution pattern is deliberately
    # case-SENSITIVE: it leans on a capitalised name to tell
    # "(Alice 2026-08-01)" from "(measured 2026-08-01)", and folding case
    # would flag every dated engineering note in the file.
    banned = [
        (r"\b[a-z0-9-]+\.ts\.net\b", re.I,
         "a private hostname or tailnet address"),
        (r"/(?:home|Users)/(?!alice\b)[a-z][a-z0-9_-]*/", re.I,
         "an absolute path into someone's home directory"),
        (r"\(\s*(?!Measured|Added|Changed|Fixed|Since|Reported|Was)"
         r"[A-Z][a-z]+ \d{4}-\d{2}-\d{2}", 0,
         "a personal attribution in a comment"),
    ]
    hits = [why for pat, flags, why in banned if re.search(pat, text, flags)]

    # Names specific to the origin deployment can't be listed in a public
    # repo, so they come from the environment. Maintainers porting a change
    # across set TACHOMETER_BANNED_WORDS to their own comma-separated list;
    # without it the structural patterns above still run for everyone else.
    private = [w.strip() for w in
               os.environ.get("TACHOMETER_BANNED_WORDS", "").split(",")
               if w.strip()]
    hits += [f"the private name {w!r}" for w in private
             if re.search(r"\b%s\b" % re.escape(w), text, re.IGNORECASE)]

    assert not hits, f"{path.name} contains {hits}"


def test_the_package_imports_with_no_private_modules_on_the_path():
    """The real check: import every module in a process that could not resolve
    a private dependency even if one were requested."""
    import importlib

    class Blocker:
        def find_module(self, name, path=None):
            return self if name.split(".")[0] in FOREIGN else None

        def load_module(self, name):
            raise ImportError(f"blocked private module: {name}")

    sys.meta_path.insert(0, Blocker())
    try:
        for mod in ("config", "claude_usage", "usage_ledger", "token_rate"):
            importlib.import_module(f"tachometer.{mod}")
    finally:
        sys.meta_path.pop(0)


def test_the_data_and_config_paths_are_xdg_not_a_hardcoded_home():
    """The origin wrote into one deployment's shared directory. A published
    tool has to land in the user's own XDG locations."""
    from tachometer import config, token_rate, usage_ledger

    for path in (token_rate.DB_FILE, usage_ledger.DATA_DIR,
                 config.CONFIG_PATH):
        assert "claude-agents" not in path, f"{path} points into the origin repo"
        assert "squad" not in path.lower(), f"{path} points into the origin repo"


def test_every_url_for_endpoint_in_the_template_exists_in_the_server():
    """The template builds URLs at render time, so a missing endpoint is not a
    broken link — it is a 500 before a single gauge draws. This is exactly how
    the first standalone render failed."""
    from tachometer.__main__ import app

    endpoints = set()
    for tpl in TEMPLATES:
        endpoints |= set(re.findall(r"url_for\('([a-z_]+)'",
                                    tpl.read_text(encoding="utf-8")))
    known = {r.endpoint for r in app.url_map.iter_rules()}
    missing = endpoints - known
    assert not missing, f"template calls url_for() for absent endpoints: {missing}"


def test_the_page_renders_without_the_hardware_rack_inventory():
    """The origin page also carried a hardware rack fed by an inventory
    module we deliberately did not carve. The panel must render without it."""
    from tachometer.__main__ import app

    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200, resp.data[:400]
    body = resp.data.decode("utf-8", "replace")
    assert 'id="tach-histspark"' in body, "the sparkline canvas is missing"
    assert "rk-metric-btn" in body, "the metric picker is missing"
