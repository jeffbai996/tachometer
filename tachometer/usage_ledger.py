"""Daily token-spend ledger — per-bot, per-Pacific-day usage rollup.

Feeds the /status fleet panel's 24h/7D/30D burn windows (reported).
The fleet scan (fleet._FLEET_SCAN) buckets every transcript entry by day
offset (0 = today, 1 = yesterday, …) for the last SCAN_DAYS days; the 300s
status sampler hands those buckets here, where they become dated rows with an
API-equivalent dollar cost (same per-model rates as fleet._cost).

Merge semantics — the reason this file exists instead of "just rescan":
transcripts get pruned, so a rescan can see FEWER tokens for an old day than
we already recorded. Today's row is overwritten (usage within a day only
grows); past days keep the max per field. That makes the ledger a monotonic
record that survives transcript pruning — real backfill on day one, honest
history afterwards.

Storage: usage_ledger.json in DATA_DIR —
  {"bots": {name: {"YYYY-MM-DD": {turns,in,out,cache_read,cache_write,cost}}}}

Writers: the status sampler only (single 300s thread) — page loads read.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from typing import Any

from . import claude_usage  # per-model rates — one accounting everywhere

DATA_DIR = os.path.expanduser(os.environ.get(
    "TACHOMETER_DATA_DIR",
    os.path.join(os.environ.get("XDG_DATA_HOME", "~/.local/share"),
                 "tachometer")))
os.makedirs(DATA_DIR, exist_ok=True)
LEDGER_FILE = os.path.join(DATA_DIR, "usage_ledger.json")

RETENTION_DAYS = 3650  # keep dailies ~forever — the all-time window needs them
                       # and a decade of rows is ~9 bots × 365 × ~100B ≈ 300KB
SCAN_DAYS = RETENTION_DAYS  # accepts full-history sources such as Codex sessions;
                            # Claude fleet scans still emit only their last 30d

_FIELDS = ("turns", "in", "out", "cache_read", "cache_write", "cost")


def _pt_today() -> date:
    """Today in the machine's LOCAL timezone.

    "Today" has to mean the day the person reading the panel is living in, or
    the daily buckets roll at a time that feels arbitrary — this was pinned to
    one deployment's timezone before it was carved out. Falls back to UTC only
    if the local zone cannot be determined at all.
    """
    from datetime import datetime
    try:
        return datetime.now().astimezone().date()
    except Exception:
        from datetime import timezone
        return datetime.now(timezone.utc).date()


def load() -> dict[str, Any]:
    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("bots"), dict):
            raise ValueError("bad shape")
        return data
    except (OSError, ValueError):
        return {"bots": {}}


def save(data: dict[str, Any], *, today: date | None = None) -> None:
    """Atomic write; prunes rows past the retention horizon on the way out."""
    today = today or _pt_today()
    horizon = (today - timedelta(days=RETENTION_DAYS)).isoformat()
    for name, days in data.get("bots", {}).items():
        data["bots"][name] = {d: row for d, row in days.items() if d >= horizon}
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".usage_ledger.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, LEDGER_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _day_totals(models: dict[str, list]) -> dict[str, float]:
    """Collapse a {model: [in, out, cache_write, cache_read, turns]} bucket into
    one dated row with cost. Bucket order is the fleet scan's; _cost takes
    cache_read THIRD, cache_write FOURTH (see fleet's cread/cwrite swap fix)."""
    row = {k: 0 for k in _FIELDS}
    row["cost"] = 0.0
    for model, v in models.items():
        row["in"] += int(v[0]); row["out"] += int(v[1])
        row["cache_write"] += int(v[2]); row["cache_read"] += int(v[3])
        row["turns"] += int(v[4])
        row["cost"] += claude_usage._cost(model, v[0], v[1], v[3], v[2])
    row["cost"] = round(row["cost"], 4)
    return row


def record_scan(bot: str, day_buckets: dict[str, dict[str, list]] | None,
                *, today: date | None = None) -> None:
    """Fold one bot's scan (day-offset → model → token bucket) into the ledger.
    Offset 0 (today) overwrites; older days merge by max per field."""
    if not day_buckets:
        return
    today = today or _pt_today()
    data = load()
    days = data["bots"].setdefault(bot, {})
    for off_str, models in day_buckets.items():
        try:
            off = int(off_str)
        except (TypeError, ValueError):
            continue
        if off < 0 or off > SCAN_DAYS:
            continue
        key = (today - timedelta(days=off)).isoformat()
        new = _day_totals(models)
        if off == 0 or key not in days:
            days[key] = new
        else:
            old = days[key]
            days[key] = {k: max(old.get(k, 0), new[k]) for k in _FIELDS}
    save(data, today=today)


def record_rows(rows: list[dict[str, Any]]) -> None:
    """Sampler hook: fold every fleet row that carries day buckets."""
    today = _pt_today()
    for r in rows:
        try:
            record_scan(r["name"], r.get("days_raw"), today=today)
        except Exception:  # noqa: BLE001 — ledger is best-effort telemetry
            continue


def window(bot: str, days_back: int | None, *, today: date | None = None,
           data: dict[str, Any] | None = None) -> dict[str, float]:
    """Totals over the last `days_back` days, today inclusive.
    days_back=None → all recorded days (the all-time window)."""
    today = today or _pt_today()
    days = (data or load())["bots"].get(bot, {})
    start = ("" if days_back is None
             else (today - timedelta(days=days_back - 1)).isoformat())
    end = today.isoformat()
    out = {k: 0 for k in _FIELDS}
    out["cost"] = 0.0
    for d, row in days.items():
        if start <= d <= end:
            for k in _FIELDS:
                out[k] += row.get(k, 0)
    out["cost"] = round(out["cost"], 2)
    return out


def series(bot: str, days_back: int, *, today: date | None = None,
           data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Exactly `days_back` points, zero-filled, oldest → newest."""
    today = today or _pt_today()
    days = (data or load())["bots"].get(bot, {})
    pts = []
    for i in range(days_back - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = days.get(d) or {}
        pts.append({"date": d, "cost": round(row.get("cost", 0.0), 2),
                    "turns": row.get("turns", 0), "out": row.get("out", 0)})
    return pts


def fleet_range(start: str, end: str, *,
                data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fleet totals per day across an explicit ISO date range, inclusive.

    Backs telemetry's long windows (30d/90d) and its custom two-date picker.
    token_rate's buffer is a 24h rate GAUGE — the existing 3d/7d/14d buttons
    already clamp to it — so anything past a day has to read the ledger, at
    daily granularity, which is the resolution that actually exists back here.

    Zero-filled: a quiet day is a real zero and must occupy a point, or the
    chart joins across the gap and implies burn that never happened. Returns
    [] when start > end rather than swapping the ends — a backwards range is a
    mis-dragged calendar, and silently charting some other window is worse than
    charting nothing."""
    if not start or not end or start > end:
        return []
    bots = (data or load()).get("bots", {})
    try:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
    except ValueError:
        return []
    pts: list[dict[str, Any]] = []
    span = (d1 - d0).days
    for i in range(span + 1):
        iso = (d0 + timedelta(days=i)).isoformat()
        cost = 0.0
        turns = 0
        out = 0
        for days in bots.values():
            row = days.get(iso)
            if not row:
                continue
            cost += row.get("cost", 0.0)
            turns += row.get("turns", 0)
            out += row.get("out", 0)
        pts.append({"date": iso, "cost": round(cost, 2),
                    "turns": turns, "out": out})
    return pts


def first_date(bot: str, *, data: dict[str, Any] | None = None) -> str | None:
    """Earliest recorded day for a bot — the honest 'data since' label."""
    days = (data or load())["bots"].get(bot, {})
    return min(days) if days else None


def fleet_first_date(*, data: dict[str, Any] | None = None) -> str | None:
    """Earliest day recorded for ANY bot — the floor a calendar may offer."""
    firsts = [min(d) for d in (data or load()).get("bots", {}).values() if d]
    return min(firsts) if firsts else None


def estimate_window(bot: str, days_back: int | None, *,
                    today: date | None = None,
                    data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Spend over a window that may reach BEFORE the ledger's first recorded day
    — real recorded cost for the covered part, PLUS a run-rate estimate for the
    uncovered pre-ledger tail (reported: 90d/180d/YTD/all all read the
    same $ because the ledger only starts 2026-06-05; the pre-June spend was in
    transcripts long since pruned, so it can't be recovered — only estimated).

    Method: recorded = window(bot, days_back). If the window's start predates the
    bot's first recorded day, the gap days are valued at the bot's observed
    $/recorded-day run-rate. The estimate is capped at the bot's own first date —
    a bot can't have spent before it existed, so we never project a bot back past
    its earliest transcript.

    Returns {recorded, estimated, total, gap_days, has_estimate, since}. The
    caller shows the full `total` as one figure. days_back None (all-time)
    extrapolates back to the FLEET EPOCH — Jan 1 of the earliest recorded year —
    so 'all' ≥ 'YTD' holds (both project over the pruned pre-June period) rather
    than 'all' reading LESS than YTD by counting only recorded days."""
    today = today or _pt_today()
    days = (data or load())["bots"].get(bot, {})
    recorded = window(bot, days_back, today=today, data=data)["cost"]
    first = first_date(bot, data=data)
    out = {"recorded": recorded, "estimated": 0.0, "total": recorded,
           "gap_days": 0, "has_estimate": False, "since": first}
    if not first or not days:
        return out
    first_d = date.fromisoformat(first)
    if days_back is None:
        # all-time → extrapolate to Jan 1 of the fleet's earliest recorded year
        win_start = date(first_d.year, 1, 1)
    else:
        win_start = today - timedelta(days=days_back - 1)
    if win_start >= first_d:
        return out                       # window fully covered by real data
    # gap = calendar days from the window start up to (not incl.) first recorded
    gap_days = (first_d - win_start).days
    rec_days = len(days)
    rate = (recorded / rec_days) if rec_days else 0.0   # $/recorded-day
    est = round(rate * gap_days, 2)
    out.update({"estimated": est, "total": round(recorded + est, 2),
                "gap_days": gap_days, "has_estimate": est > 0})
    return out


def fleet_lifetime_cost(*, data: dict[str, Any] | None = None,
                        today: date | None = None) -> dict[str, Any]:
    """Every dollar the fleet has spent since recording began — the tachometer's
    odometer reading.

    Sums estimate_window(bot, None) across every bot, so it is EXACTLY the
    figure the /status fleet table foots to on its 'all' window (both call the
    same per-bot estimator). Keeping one implementation is the point: two
    independent sums of the same ledger would drift the moment either one's
    extrapolation rule changed.

    Returns {total, recorded, estimated, bots, since} — `since` is the fleet's
    earliest recorded ledger day, i.e. what "since we started recording" means.
    """
    data = data if data is not None else load()
    today = today or _pt_today()
    recorded = estimated = 0.0
    firsts: list[str] = []
    for bot in data.get("bots", {}):
        e = estimate_window(bot, None, today=today, data=data)
        recorded += float(e["recorded"])
        estimated += float(e["estimated"])
        if e.get("since"):
            firsts.append(e["since"])
    return {
        "total": round(recorded + estimated, 2),
        "recorded": round(recorded, 2),
        "estimated": round(estimated, 2),
        "bots": len(data.get("bots", {})),
        "since": min(firsts) if firsts else None,
    }
