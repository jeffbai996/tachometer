"""tachometer — a live instrument panel for Claude Code token burn.

Serves the panel and the four endpoints it polls. Single file on purpose: the
whole point of the project is that you point it at a directory and it works,
so the server should be something a reader can hold in their head.

    python -m tachometer              # -> http://localhost:871
    python -m tachometer --doctor     # what did it actually find?
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from flask import Flask, jsonify, render_template, request

from . import claude_usage, config, token_rate, usage_ledger

app = Flask(__name__)

# Window label -> seconds. The panel's own buttons send these labels, so the
# mapping lives server-side and an unknown label falls back to an hour rather
# than erroring — a bad label should degrade the view, not break the page.
WINDOWS: dict[str, int] = {
    "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400,
    "7d": 604800, "30d": 2592000, "90d": 7776000,
}


@app.route("/")
def index() -> str:
    # `snap` is the hardware-rack inventory (per-machine CPU/GPU) that shared a
    # page with the tachometer where this was carved from. That is a different
    # instrument with different inputs, and it is not part of this project — an
    # empty host list renders the panel alone, which is what we want.
    return render_template("tachometer.html",
                           snap={"hosts": [], "cache_ttl_sec": 1})


@app.route("/api/token-rate")
def api_token_rate() -> Any:
    out = token_rate.current_rate()
    try:
        out.setdefault("fleet", {})["lifetime_usd"] = \
            usage_ledger.fleet_lifetime_cost()
    except Exception:
        # The odometer is a nicety; a missing ledger must not take the dial
        # down with it. The client holds its last value when the key is absent.
        pass
    return jsonify(out)


@app.route("/api/token-rate/history")
def api_token_rate_history() -> Any:
    window = request.args.get("window", "1h")
    since = WINDOWS.get(window, 3600)
    return jsonify(token_rate.history(
        since_sec=min(since, token_rate.RETENTION_SEC)))


@app.route("/api/token-rate/daily")
def api_token_rate_daily() -> Any:
    """Per-day totals from the ledger — the long-window source.

    The rate gauge keeps a bounded rolling buffer, so it cannot answer 30d or
    90d: those windows clamp and report "still filling". The ledger keeps
    dailies indefinitely, so anything past the buffer reads from here instead.
    """
    from datetime import timedelta
    days = max(1, min(int(request.args.get("days", "30")), 3650))
    today = usage_ledger._pt_today()
    start = (today - timedelta(days=days - 1)).isoformat()
    return jsonify({"points": usage_ledger.fleet_range(start,
                                                       today.isoformat())})


# ── hardware telemetry ───────────────────────────────────────────────────
# The panel was carved out of a page that also carried a hardware rack
# (per-machine CPU/GPU/power). That is a separate instrument with separate
# inputs and it is not part of this project — but the template still asks the
# router for these endpoints while building its URLs, so they have to EXIST or
# the page 500s before a single gauge renders.
#
# They return empty rather than fabricated readings: the panel already handles
# "no hosts" by drawing nothing, and inventing a plausible-looking CPU number
# would be worse than an empty section.

@app.route("/api/telemetry")
def api_telemetry() -> Any:
    return jsonify({"hosts": [], "ts": time.time()})


@app.route("/api/telemetry/history")
def api_telemetry_history() -> Any:
    return jsonify({"hosts": {}, "ts": time.time()})


@app.route("/api/telemetry/history/stats")
def api_telemetry_history_stats() -> Any:
    return jsonify({"hosts": {}, "ts": time.time()})


def doctor() -> int:
    """Print what the scanner actually found.

    Most "it shows nothing" reports are a config directory somewhere
    non-default, which is invisible from the panel — it just looks broken.
    One command that says where it looked and what it saw answers that before
    anyone opens an issue.
    """
    cfg = config.load()
    projects = os.path.join(os.path.expanduser(cfg["config_dir"]), "projects")
    print(f"config file   {config.CONFIG_PATH}")
    print(f"config_dir    {cfg['config_dir']}")
    print(f"projects      {projects}")
    if not os.path.isdir(projects):
        print("\n  NOT FOUND. Claude Code stores transcripts in "
              "<config_dir>/projects.")
        print("  Set config_dir in the config file above, or set "
              "CLAUDE_CONFIG_DIR.")
        return 1

    res = claude_usage.compute(projects)
    print(f"transcripts   {res['files']} files, {res['turns']} turns")
    if not res["turns"]:
        print("\n  Found the directory but no usage records in it. If this is "
              "a fresh install, run Claude Code once and try again.")
        return 1

    print(f"est. cost     ${res['total']['cost']:,.2f}")
    print("\nmodels seen:")
    known, unknown = [], []
    for model, slot in sorted(res["by_model"].items(),
                              key=lambda kv: -kv[1]["cost"]):
        (known if claude_usage.rate_for(model) else unknown).append(
            (model, slot))
    for model, slot in known:
        print(f"  {model:32} ${slot['cost']:10,.2f}")
    for model, slot in unknown:
        # Priced at zero and silently absent from every total — worth saying
        # out loud, since the symptom is "the numbers look low" with no error.
        print(f"  {model:32} {'NO RATE — counted as $0':>10}")
    if unknown:
        print(f"\n  {len(unknown)} model(s) have no rate configured. Add them "
              f"under [rates] in\n  {config.CONFIG_PATH} to include them in "
              "the totals.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tachometer", description=__doc__)
    ap.add_argument("--port", type=int, default=871,
                    help="port to serve on (default: 871)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: 127.0.0.1)")
    ap.add_argument("--doctor", action="store_true",
                    help="print what the scanner found, then exit")
    args = ap.parse_args()

    if args.doctor:
        return doctor()

    config.load()          # writes the default config on first run
    token_rate.start_sampler()
    print(f"tachometer -> http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
