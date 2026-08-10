"""Token-burn rate — the live tachometer's data source.

`usage_ledger.py` answers "what was spent today/this week" and the transcript
scan answers "what has each session burned so far". Neither can answer "how
hard is this working RIGHT NOW", because both keep only cumulative or per-day
totals — a rate needs a time base, and day-buckets throw it away.

This module keeps that time base: it samples the fleet's cumulative token
counters every SAMPLE_INTERVAL_SEC and stores (host, ts, cumulative) rows.
The rate is then the DIFFERENCE between consecutive samples divided by the
elapsed time — a derivative, computed at read time so the stored data stays
raw and re-interpretable.

Why differencing cumulative counters instead of summing per-turn timestamps:
the transcripts are the source of truth and they're already scanned once per
tick by the session scan. Re-reading every transcript to timestamp individual
turns would cost an order of magnitude more IO for the same answer.

THE HARD PART is that a cumulative counter is only *mostly* monotonic:

  - `cost_today`/`today` reset to zero at Pacific midnight
  - transcripts get pruned, so even an all-time total can shrink
  - a bot can appear mid-stream carrying millions of tokens of history
  - the sampler can die, leaving a stale pair that would freeze the needle

Every one of those, differenced naively, produces a garbage needle — a huge
negative swing, or a phantom burst of a bot's entire history in one interval.
So `_delta()` treats any DECREASE as a counter reset (contributing 0, not a
negative), unknown predecessors contribute nothing, and reads older than
STALE_AFTER_SEC report zero with `stale: true` rather than a frozen value.

Retention is RETENTION_SEC (7 days as of 2026-08-05, was 24h): enough to back
the sparkline at minute resolution out to a week. Beyond that usage_ledger owns
the history, at one row per calendar day.

Writers: the sampler thread only (started by server.main(), never at import).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Optional


# The rolling sample store. XDG data dir, created on first use — this is a
# cache of recent samples, not user data: deleting it costs you the sparkline's
# recent history and nothing else.
DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "tachometer")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "token_rate.db")

# 5s: fast enough that the needle reads as live, slow enough that the local
# transcript scan (cheap file reads) stays background noise. Was 10s; halved
# 2026-07-31 ("make the tachometer a lot more responsive") — the dial polls at
# 2s, so the sampler is what bounds how quickly a burst shows up. Remote bots
# refresh on the 300s fleet tick regardless — see `live` in current_rate().
SAMPLE_INTERVAL_SEC = int(os.environ.get("TACHOMETER_INTERVAL_SEC", "5"))
# ── adaptive cadence (reported: "should refresh a lot faster when
# there's activity, to behave like an actual tachometer") ──
# The UI already polls at 2s, so the SAMPLER was the ceiling on responsiveness:
# polling a 5s sample at 2s just re-reads the same row 2-3x. But sampling at 1s
# unconditionally means scanning transcripts every second forever, and the
# fleet is idle most of the day — pure IO burn for a needle sitting at zero.
# So the interval tracks the work: fast while anything is burning, decaying
# back to the idle rate after the burn stops. IDLE_AFTER_SEC of quiet (not one
# empty tick) triggers the backoff, so a pause between turns doesn't yo-yo the
# sampler.
#
# MEASURED, not chosen. A local-only scan costs 1.2-2.2s depending on load:
# every bot's row spawns a `python3 -c` interpreter (~0.57s apiece) and they
# fan out across threads. That is FAR too expensive to run on a tachometer
# cadence — an attempt at 2s sampling measured 228% CPU and wedged the Flask
# API so hard that /api/token-rate stopped answering at all, because the
# sampler shares a process with the request threads.
#
# So the sampler stays at its original 5s and there is no adaptive speed-up:
# the responsiveness ceiling here is the SCAN COST, and no interval tuning
# gets around a 2s scan. Making the needle genuinely faster needs a cheaper
# source (incremental transcript tailing, or the bots reporting their own
# counters) rather than a tighter loop. The UI covers the gap by tweening
# between samples, which is what makes the motion continuous.
# 5 -> 3s while something is burning (reported: "update the numbers in
# the annunciator panel at a faster clip"). The panel's figures come straight off
# these samples, so the sampler IS their refresh rate — the UI already repaints
# at 250ms and was just redrawing the same value four times over.
#
# 3s is the floor worth taking: a local scan measures 2.21s, so anything tighter
# has the loop running essentially back-to-back with no idle gap, and the note
# above still holds — the ceiling is scan COST, not interval. Idle stays at 5s;
# a quiet fleet has nothing to report faster.
ACTIVE_INTERVAL_SEC = float(os.environ.get("TACHOMETER_ACTIVE_SEC", "3"))
IDLE_INTERVAL_SEC = float(os.environ.get("TACHOMETER_IDLE_SEC", "5"))
IDLE_AFTER_SEC = float(os.environ.get("TACHOMETER_IDLE_AFTER_SEC", "20"))
# 7d of rate history backs the dial's sparkline (24h before — reported: "for
# 3d and 7d still use minute resolution on sparklines"). At 24h the 3d and
# 7d selections had to fall back to usage_ledger's per-DAY buckets, and the jump
# from minute resolution to daily bars read as a different chart rather than a
# longer view of the same one.
#
# Cost, measured at the old ceiling: 24.1h held 114,941 rows in 12.2MB, so 7d
# lands around 800k rows and ~85MB. Real but affordable for a local sqlite file
# pruned hourly. 14d+ deliberately stays on usage_ledger — that is where the
# resolution SHOULD step down, and the ledger keeps dailies for ~10 years.
#
# This only governs what is KEPT from now on. It cannot backfill: the window
# fills at wall-clock speed, so 7d of history exists 7 days after this ships.
RETENTION_SEC = int(os.environ.get("TACHOMETER_RETENTION_SEC", str(7 * 86400)))
# A pair older than this can't describe the CURRENT burn. Generous multiple of
# the sample interval so an ordinary hiccup doesn't blank the gauge, but far
# below the 300s remote tick so a truly dead sampler is caught.
STALE_AFTER_SEC = int(os.environ.get("TACHOMETER_STALE_SEC", "120"))
# Bots whose tokens are billed by OpenAI rather than Anthropic — the vendor
# split on the sparkline. `gpt` is the Discord bot (Codex CLI engine) and
# `codex` is the user's personal CLI/Desktop usage; both spin the same meter.
# A NAME list, not an engine probe: the roster's `engine` field is only
# populated for live rows, and history has to classify bots that stopped
# reporting days ago.
CODEX_BOTS = frozenset({"gpt", "codex"})
# Window the NEEDLE averages over. The counters come from transcript scans and
# usage lands only when a message COMPLETES, so a minute of generation shows up
# as one instantaneous counter jump. Differencing adjacent samples turned that
# into a single 3-5s spike (154k "tok/s") followed by zeros while the next
# message was still being written — the dial read 0 through sustained work and
# looked stuck (reported). A short trailing window converts the jumps
# into the sustained-equivalent rate: steady value while a bot works. The
# grace+fade pair below owns the wind-down once it stops.
# 15s, halved from 30 (reported: "up the refresh rate on that one a
# tad"). The response cache is 0.2s and the page polls at 250ms, so NEITHER was
# the limiter — this window is. At 30s a burst took half a minute to fully
# register in the rate tile. The active sampler ticks every 3s, so 15s still
# averages ~5 samples: fast enough to feel live, wide enough that a single
# counter jump doesn't spike it back to the pre-window behaviour.
RATE_WINDOW_SEC = int(os.environ.get("TACHOMETER_RATE_WINDOW_SEC", "15"))
# Wind-down after the counter stops moving. The plateau estimator holds a
# turn's average rate, but its natural 1/t decay is far too slow — the last
# reading just sat on the dial ("the tach just stays stuck at a number", the user
# 2026-07-31). Instead: full value while the quiet time is within GRACE (an
# ordinary between-message gap), then a linear fade to zero over FADE. Work
# holds the needle; ~90s of silence returns it to rest.
RATE_GRACE_SEC = float(os.environ.get("TACHOMETER_GRACE_SEC", "45"))
RATE_FADE_SEC = float(os.environ.get("TACHOMETER_FADE_SEC", "45"))
# How far back current_rate() reads. It exists purely to stop the query
# fetching the whole table: that pulled 79,012 rows to use a handful, and eight
# concurrent pollers each building that list is what hung the endpoint and
# blanked the page (reported).
#
# Sized by the LONGEST-reaching consumer, not by RATE_WINDOW_SEC. The needle
# only differences inside a 30s window, but the $/hr estimator falls back to a
# bot's OLDEST retained sample when nothing newer is far enough apart, so it can
# legitimately span a long quiet stretch — a 600s bound broke a 1800s cost span
# in the existing tests. An hour covers that with room to spare while still
# reading a small, bounded slice instead of the table; and if a window ever does
# come back empty, the fallback query below re-reads the newest pair per bot so
# staleness is still reported honestly.
_LOOKBACK_SEC = float(os.environ.get(
    "TACHOMETER_LOOKBACK_SEC", str(max(RATE_WINDOW_SEC * 120, 3600))))
# Sanity ceiling for a single bot's token rate. The counters ride TWO scan
# sources (local tick + remote rescan + the 300s status snapshot), and around
# the Pacific-midnight bucket reset they can disagree: the fresh source reads
# 0, then a staler source re-asserts YESTERDAY'S total seconds later — the
# differencer sees a bot's whole day as one 4-second burst, and the sparkline
# grows a 1.6M tok/s spike (reported: "peak 1.6M tok/sec which i dont
# think is possible"). Correct. The honest per-bot ceiling is a full-context
# ingest spread over its turn — ~1M fresh tokens over a minute, ~17k/s — so
# any jump implying more than 30k tok/s is a counter anomaly, not work:
# re-anchor on the new value and paint nothing, exactly like a reset.
MAX_SANE_BOT_TOK_S = float(os.environ.get("TACHOMETER_MAX_TOK_S", "30000"))
# Same governor for the money staircase — the midnight replay moves dollars
# too (a bot's whole day re-differenced over seconds; $900 replayed across the
# 300s snapshot gap is $3/s, which a loose cap waved through as a $10k/hr
# sparkline spike). An expensive single turn flushes a few dollars over tens
# of seconds — $0.3/s-ish — so $1/s per bot still clears every honest reading
# with margin while catching the replay.
MAX_SANE_BOT_USD_S = float(os.environ.get("TACHOMETER_MAX_USD_S", "1"))

# Hosts whose numbers arrive via the 300s ssh fleet tick rather than a local
# How often the token sampler re-scans REMOTE bots itself, rather than reusing
# whatever the 300s status snapshot last left behind (reported: "ssh
# 300s should prolly poll more"). Kept as its own knob instead of lowering
# status.SAMPLE_INTERVAL_SEC, because that tick also drives service health and
# the full host sweep — speeding all of it up would multiply ssh load for
# reasons unrelated to the tachometer. One round-trip per remote bot per
# minute is affordable; the same scan every 5s would not be.
# 0 disables the sampler's own remote scan entirely (the default): remote rows
# are spliced from the status snapshot instead. A dedicated 60s ssh refresh was
# tried and reverted — combined with the scan cost it kept three ssh children
# alive almost continuously, which is both the documented no-barrage rule and a
# real contributor to the API wedging.
# 60s, not 0: disabling this left remote bots riding the 300s status snapshot
# only, which the user immediately noticed as "back to 300s on ssh" (2026-07-31).
# One ssh round-trip per remote bot per minute is the agreed budget.
#
# ADAPTIVE (reported: "poll the SSH more than 60s, its a bit not
# granular enough"). A flat cut is exactly the change that was tried and
# reverted — three remote hosts x a continuous scan is the ssh barrage that
# helped wedge the API. So the budget is spent where it buys resolution
# instead: while a remote bot is actively burning, refresh at ACTIVE; once
# they all go quiet, fall back to the 60s baseline. A quiet fleet is the common
# case, so average ssh load stays at the old level while an active remote bot
# gets 2x the granularity on the dial.
#
# 15s is affordable ONLY because fleet._scan now caches its uploaded scan
# script on the remote host (content-addressed by hash). Before that, a WSL
# host re-uploaded ~6.6KB in 4 chunk writes + a finalize on EVERY scan — 6 ssh
# round-trips each — and a full bot_rows() took 4.83s. It now takes 1.10s
# steady-state (measured 2026-08-01), so:
#     15s active -> 7.3% ssh duty cycle
#     60s (the OLD baseline, uncached) -> 8.1%
# i.e. polling 4x more often now costs LESS than the original setting did.
# Do not lower this further without re-measuring bot_rows(): the constraint is
# duty cycle, not the interval, and the documented failure mode is ssh children
# alive near-continuously. Below ~10s, push beats polling — see option 1 in the
# 2026-08-01 discussion (remote agents POST deltas over Tailscale).
# 60 -> 30 (reported: a a session turn "took a long ass time" to appear).
# The ACTIVE cadence can't help with FIRST detection — a bot only counts as
# active once its counter has been seen moving, so the first sighting of a
# burst always waits a full IDLE interval. That was the 60s.
#
# Re-measured before changing it, as the note above demands: bot_rows() is
# 2.3-4.1s right now, NOT the 1.10s recorded above — the box is under IO
# pressure and the scan is slower for it. At 2.3s:
#     30s idle -> 7.7% duty   (just inside the ~8.1% documented ceiling)
#     20s idle -> 11.5%       (over budget)
# 30s was tried first and backed off to 45s the same day: the duty-cycle sum
# above counts only THIS scan, but the telemetry host-probe (GPU/CPU per host)
# opens its own ssh children on a separate cadence. Sampled live at 30s, ssh
# children were present in ~half of a 20s window, peaking at 3 concurrent —
# comfortably past the "children alive near-continuously" failure mode the note
# above warns about, which is why this sat at 45s for a while.
#
# Back to 30s at the user's call (2026-08-01: "didnt we make it 30s when inactive").
# The duty cycle above is the known cost and it is his to accept; the idle
# cadence is what decides how stale a quiet remote bot's row can get, and 45s
# was a compromise made on my side of that trade. If the box starts feeling it,
# the fix is still to make the two probes share one connection rather than to
# stretch either interval.
REMOTE_REFRESH_SEC = float(os.environ.get("TACHOMETER_REMOTE_SEC", "30"))
REMOTE_ACTIVE_SEC = float(os.environ.get("TACHOMETER_REMOTE_ACTIVE_SEC", "15"))
# How long a remote bot counts as "recently burning" after its counter last
# moved. Comfortably longer than one ACTIVE interval so a bot mid-turn doesn't
# flip back to the slow cadence between two of its own flushes.
REMOTE_ACTIVE_FOR_SEC = float(os.environ.get("TACHOMETER_REMOTE_ACTIVE_FOR_SEC", "120"))

# Current remote-scan cadence, published by the sampler for the UI. A list so
# the sampler thread can swap it without a global declaration; a stale read is
# harmless (worst case the footer shows the previous cadence for one poll).
_remote_gap: list = [REMOTE_REFRESH_SEC]

_init_lock = threading.Lock()
# single-slot cache for current_rate(): (monotonic-ish wall ts, payload).
# List so the closure can swap it without a global statement; races between
# readers are benign (worst case: two threads compute once each).
_rate_cache: list = [None]
# Single-flight guard for the cache above. The coalescing it promises only
# worked for callers arriving AFTER a value was cached; on a cold or just-
# expired entry every concurrent poller missed together and each ran its own
# read. Holding this lock across the miss means one thread computes and the
# others take its result (reported, black-screen investigation).
_rate_flight = threading.Lock()


def _connect() -> sqlite3.Connection:
    """Open the rate DB with schema ensured. Mirrors telemetry_history._connect:
    schema on every connect (idempotent, survives a crash between file creation
    and CREATE TABLE), WAL so the sampler and readers don't block, and a
    busy_timeout so a lock returns late instead of 500ing the page."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        with _init_lock:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS samples ("
                "  bot TEXT NOT NULL,"
                "  ts REAL NOT NULL,"
                "  host TEXT,"
                "  tokens REAL,"   # cumulative WORK tokens: fresh in + out (see _cumulative)
                "  cost REAL,"     # cumulative dollars (today's bucket)
                "  PRIMARY KEY (bot, ts)"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokrate_bot_ts "
                "ON samples (bot, ts)"
            )
    except Exception:
        conn.close()
        raise
    return conn


def _cumulative(row: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """One fleet.bot_rows() row → (cumulative_work_tokens, cumulative_cost).

    WORK TOKENS = output + fresh input. BOTH cache lanes are excluded on
    purpose, and that exclusion is the whole point of this function.

    The first version summed all four buckets and produced a needle reading
    112k tok/s on an idle box (reported: "112k tok/sec is impossible").
    It wasn't a division bug — it was the wrong quantity. A long-running agent
    re-reads its ENTIRE context from cache on every single turn, so cache_read
    per turn ≈ context size. Measured live on a session: 423,966 cache_read
    per turn against a 485k context, while it generated 1,031 output tokens per
    turn. Summing cache_read therefore measures "context size × turn rate" —
    a bot with a big context looks like it's redlining while sitting idle, and
    the number grows when you compact LESS, which is backwards.

    Cache WRITES fell to the same argument one day later (reported: "a remote session just answered a few queries and its used like 6 million
    tokens"). A Discord bot idles past the cache TTL between queries, so every
    query RE-WRITES its whole context: measured live on a remote session, 1.19M
    cache_write for a handful of answers, while cache-warm interactive bots
    (a session) showed the intuitive number. cache_write therefore measures
    "context size × session rate" — the same context-size artifact as reads,
    only keyed to idle gaps instead of turns, and it punishes exactly the bots
    that idle politely.

    Both lanes still cost real money (reads 10%, writes 125% of input rate),
    so they stay in `cost` and keep every $ readout honest — and "money moving
    while the needle reads zero" is precisely what the dial's CACHE annunciator
    exists to show. Tokens mean work; dollars mean money.

    A row with no detail block (gemma / an errored bot) yields (None, None) so
    it's skipped rather than counted as zero — a zero would read as a drop to
    idle and then spike when it came back."""
    detail = row.get("detail")
    if not isinstance(detail, dict):
        return None, None
    try:
        fresh_in = float(detail.get("in") or 0)
        # ── gpt/codex: OUTPUT ONLY ──
        # Codex's inputTokens is the whole re-sent context, and its
        # cachedInputTokens counts only the part that HIT cache. Subtracting
        # them (the first fix, 2026-08-01) still leaves the uncached remainder
        # of the context — which is a context-size artifact, not work, exactly
        # like the cache lanes above. Measured across six days it is a stable
        # 80k-123k per turn at an 81-90% hit rate, i.e. it tracks how big the
        # context is, not how much was done. For scale, on the same day every
        # Claude bot reported 2 fresh input tokens per turn; gpt reported
        # 70,003 (reported: "it just did one turn and here it says 449k
        # tok lol").
        #
        # There is no field in codex's stats that isolates genuinely-new input,
        # so the honest measure of gpt's work is its OUTPUT, which is real
        # generated tokens either way. Costs are unaffected — cost_today still
        # bills every input lane.
        if detail.get("kind") == "gpt":
            fresh_in = 0.0
        tokens = fresh_in + float(row.get("out_today") or 0)
        cost = float(row.get("cost_counter", row.get("cost_today")) or 0)
    except (TypeError, ValueError):
        return None, None
    return tokens, cost


def record(rows: list[dict[str, Any]], *, now: float | None = None) -> int:
    """Persist one fleet snapshot's cumulative counters. Returns rows written.

    Best-effort by contract: a malformed row is skipped, never raised, because
    this runs on the sampler thread where an exception would kill the loop."""
    ts = time.time() if now is None else now
    payload = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not name:
            continue
        tokens, cost = _cumulative(row)
        if tokens is None:
            continue
        tokens, cost = _monotonic_gate(name, ts, tokens, cost)
        payload.append((name, ts, row.get("host"), tokens, cost))
    if not payload:
        return 0
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO samples (bot, ts, host, tokens, cost) "
            "VALUES (?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return len(payload)


# ── stale-source suppression (reported: "a remote session is back to 4.8M") ──
# Remote bots' counters arrive from TWO sources on different clocks: the
# dedicated 60s ssh rescan (fresh) and the 300s status snapshot (up to five
# minutes stale). The sampler used to write whichever spoke last, so the
# stored series OSCILLATED — 1,209,055 / 1,188,997 / 1,209,055 … and around
# midnight 0 / 1.17M / 0 / 1.17M — and every re-assert after a stale dip
# painted the difference as brand-new work. The rate governor can't catch it:
# jump-spreading dilutes a replayed megatoken over the preceding flat run to a
# few hundred tok/s, comfortably "sane". Double-counting at the source is why
# a remote session read 4.8M for an evening of light chat.
#
# The gate exploits what a TODAY-bucketed counter can't do: legitimately go
# DOWN mid-day. A lower reading is therefore a stale source until proven
# otherwise — proven by PERSISTING for DROP_ACCEPT_SEC without any higher
# re-assert (a real reset — midnight, transcript pruning, a definition change
# — sticks; a stale snapshot keeps getting contradicted by the fresh rescan).
# While a drop is on probation the sampler re-writes the last accepted value,
# so the stored series stays flat instead of sawtoothing. Worst case a real
# reset is recorded DROP_ACCEPT_SEC late, which at 90s is invisible on a
# day-scale bucket.
DROP_ACCEPT_SEC = float(os.environ.get("TACHOMETER_DROP_ACCEPT_SEC", "90"))
_gate_state: dict[str, dict] = {}   # bot -> {tok, cost, drop_since}


def _monotonic_gate(name: str, ts: float, tokens: float,
                    cost: float) -> tuple[float, float]:
    """Filter one bot's sample through the stale-source gate (sampler thread
    only — single writer, so the module dict needs no lock)."""
    st = _gate_state.get(name)
    if st is None or tokens >= st["tok"]:
        _gate_state[name] = {"tok": tokens, "cost": cost, "drop_since": None}
        return tokens, cost
    # lower than last accepted: probation
    if st["drop_since"] is None:
        st["drop_since"] = ts
    if ts - st["drop_since"] >= DROP_ACCEPT_SEC:
        # the drop persisted with no higher re-assert — it's a real reset
        _gate_state[name] = {"tok": tokens, "cost": cost, "drop_since": None}
        return tokens, cost
    # still on probation — hold the line at the last accepted reading
    return st["tok"], st["cost"]


def _delta(curr: Optional[float], prev: Optional[float]) -> float:
    """Difference of two cumulative readings, floored at zero.

    A DECREASE is never negative burn — it's a counter reset (Pacific midnight
    zeroing today's bucket) or transcript pruning shrinking the total. Both mean
    "we can't measure this interval", which is 0, not a backwards needle."""
    if curr is None or prev is None:
        return 0.0
    d = curr - prev
    return d if d > 0 else 0.0


def current_rate(*, now: float | None = None) -> dict[str, Any]:
    """The needle: instantaneous burn from the two most recent samples per bot.

    Returns {fleet: {tok_per_sec, tok_per_min, tok_per_hour, usd_per_hour},
             bots: [{name, host, live, tok_per_sec, usd_per_hour}], stale, ts}.
    `stale` is True when the newest sample is older than STALE_AFTER_SEC — the
    gauge then reads zero rather than freezing on the last known delta.

    Coalesces concurrent pollers: several dashboards polling a few times a
    second must not each pay for a fresh read. An explicit `now` (tests)
    bypasses the cache entirely.
    """
    if now is not None:
        return _compute_rate(now=now, live=False)

    # 0.2s TTL, tracking the dial's 250ms poll (reported; was 0.4s for
    # the 500ms poll). The rule: cache TTL stays just UNDER the poll interval.
    # Longer and every other request replays an identical reading, so the
    # faster poll buys nothing; much shorter and simultaneous dashboards stop
    # coalescing onto one read, which is the whole reason this cache exists.
    hit = _rate_cache[0]
    if hit and time.time() - hit[0] < 0.2:
        return hit[1]

    # Miss. Take the flight lock so ONE thread computes and the rest wait for
    # it — the check above is not enough on its own, because on a cold or
    # just-expired entry every concurrent poller misses together, and it was
    # eight of those each reading the full table that hung the endpoint.
    with _rate_flight:
        # Re-check inside the lock: whoever we queued behind has just filled
        # the cache, and recomputing here would defeat the coalescing.
        hit = _rate_cache[0]
        if hit and time.time() - hit[0] < 0.2:
            return hit[1]
        return _compute_rate(now=time.time(), live=True)


def _compute_rate(*, now: float, live: bool) -> dict[str, Any]:
    """The actual read behind `current_rate`. See that function for the contract.

    Split out so the cache/single-flight logic reads as one small block rather
    than being threaded through a long function with several exit points.
    """
    conn = _connect()
    try:
        # No time-window filter here. The newest pair per bot is what defines
        # the needle, and whether that pair is too OLD to trust is decided
        # below by the `stale` check against its own timestamp. Filtering by
        # `ts >= now - window` looks equivalent but silently returns "0 tok/s,
        # not stale" — indistinguishable from a genuinely idle fleet — instead
        # of "0 tok/s, STALE", which is the honest reading when the sampler has
        # stopped. Retention already bounds the table, so the scan stays small.
        # ASC on both columns so the (bot, ts) index serves the order
        # directly. The old "bot ASC, ts DESC" mixes directions, which sqlite
        # cannot satisfy from the index — a full-table SORT on every call.
        # Cheap at 6h retention; at 24h with several pollers the workers
        # stacked inside that sort (GIL released during the C sort) faster
        # than they drained — every thread pinned at token_rate line 266 in
        # the faulthandler dump, API dead at 230% CPU (2026-07-31).
        #
        # That earlier fix made the ORDER BY index-friendly, which cut the
        # constant but left the real problem: this pulled EVERY row (79,012 on
        # the local host) to use the newest two per bot — sixteen. Single-threaded
        # that materialises in ~0.09s, so it never looked expensive; but each
        # concurrent caller builds its own 79k-tuple list, and eight of those on
        # a swapping box thrash the allocator badly enough that the endpoint
        # stops answering entirely. Same dump, same pinned line, one line
        # further down (2026-08-01: the black screen on returning to a
        # backgrounded telemetry tab).
        #
        # So ask for what the needle actually needs: a TIME window, not the
        # table. The loop below walks back from each bot's newest sample only
        # until it has covered RATE_WINDOW_SEC, so anything older than that
        # cannot change the answer.
        #
        # Bounded by time rather than by a row count on purpose — a row LIMIT
        # silently becomes wrong the moment the sampler speeds up (the cadence
        # is adaptive: IDLE_INTERVAL_SEC vs the busy interval), whereas the
        # time bound is exactly the quantity the arithmetic depends on.
        # _LOOKBACK_SEC keeps a wide safety margin over RATE_WINDOW_SEC so a
        # bot sampled sparsely still has an older point to difference against,
        # and `stale` is still decided by the newest row's own timestamp.
        rows = conn.execute(
            "SELECT bot, ts, host, tokens, cost FROM samples "
            "WHERE ts >= ? ORDER BY bot ASC, ts ASC",
            (now - _LOOKBACK_SEC,),
        ).fetchall()
        if not rows:
            # Nothing inside the window: the sampler has stopped. This is the
            # case the original full-table read existed to serve, and it must
            # keep working — a time filter that returns nothing here would
            # report "0 tok/s, not stale" (indistinguishable from a genuinely
            # idle fleet) instead of the honest "0 tok/s, STALE".
            #
            # `stale` is derived from newest_ts, which is only assigned inside
            # the per-bot loop below — and that loop SKIPS any bot with fewer
            # than two samples. So fetching just the newest row per bot is not
            # enough: it would leave newest_ts at 0 and silently un-stale the
            # gauge. Take the newest PAIR per bot, which is also exactly what
            # the rate arithmetic wants.
            rows = conn.execute(
                "SELECT bot, ts, host, tokens, cost FROM samples s WHERE ts >= ("
                "  SELECT MIN(ts) FROM (SELECT ts FROM samples"
                "                       WHERE bot = s.bot ORDER BY ts DESC LIMIT 2))"
                " ORDER BY bot ASC, ts ASC"
            ).fetchall()

        # When each bot's counter last MOVED, across the whole retained
        # history rather than the lookback window above.
        #
        # This has to be window-independent: the roster decides "idle" vs
        # "inactive" from it, and a bot quiet for six hours is both the one
        # that should read inactive and the one no narrow window can see. When
        # the rate read was bounded to the lookback, a bot whose counter last
        # moved before that window had no change inside it, so last_burn came
        # back None and the roster fell through to the window's own depth and
        # kept calling it "idle" (reported: "how come all the bots say
        # idle when some of them actually should be inactive cus over 2hr").
        #
        # Answered by SQL rather than by walking rows in Python, so restoring
        # the full-retention reach costs one indexed aggregate instead of
        # bringing back the whole-table fetch this query exists to avoid: for
        # each bot, the newest timestamp whose token counter differs from the
        # sample before it.
        # WALK BACK PER BOT, STOP AT THE FIRST INCREASE — do not scan history.
        #
        # This was a LAG() window over the WHOLE samples table, which is a
        # 971k-row scan to produce ten timestamps: measured 1.18s of a 1.23s
        # cold read, i.e. 98% of it, and it grows with retention. Cached
        # requests were 2ms while a cold one took 7.7s, so an idle panel showed
        # nothing for seconds and read as broken (reported: "graph broke
        # again, think the tach is stuck too" -> "took bloody forever to load").
        #
        # The answer each bot needs is only its MOST RECENT increase, so
        # descending order finds it near the start and stops: an active bot
        # breaks after two rows, and an idle one walks back exactly as far as
        # its last burn rather than to the beginning of retention. Same 44x on
        # the measured table (1.18s -> 0.027s), and verified to return results
        # identical to the window function on live data before replacing it.
        #
        # `newer[1] > tok`, NOT `!=`. The counters are TODAY-bucketed, so at
        # Pacific midnight every one of them DROPS from yesterday's total to
        # ~0. A `!=` match reads that decrease as a burn, which is why the
        # whole roster claimed "idle 11m" a few minutes after midnight while
        # most bots had not run for hours — a session's real last burn was 04:11
        # the previous day (reported: "all the bots show idle 11m when
        # a lot of them are actually inactive"). Only an INCREASE is work.
        # Reading descending, the increase is between `newer` and the row
        # under it, so the burn's timestamp is the NEWER one.
        last_burn: dict[str, float] = {}
        for (b_name,) in conn.execute(
                "SELECT DISTINCT bot FROM samples").fetchall():
            newer: tuple[float, float] | None = None
            for b_ts, b_tok in conn.execute(
                "SELECT ts, tokens FROM samples WHERE bot = ? ORDER BY ts DESC",
                (b_name,),
            ):
                if newer is not None and newer[1] > b_tok:
                    last_burn[b_name] = float(newer[0])
                    break
                newer = (b_ts, b_tok)
    finally:
        conn.close()

    # newest-first per bot: rows arrive oldest-first per bot, so reverse each
    per_bot: dict[str, list[tuple]] = {}
    for r in rows:
        per_bot.setdefault(r[0], []).append(r)
    for lst in per_bot.values():
        lst.reverse()

    newest_ts = 0.0
    bots: list[dict[str, Any]] = []
    fleet_tok = 0.0
    fleet_usd = 0.0

    for name, samples in per_bot.items():
        if len(samples) < 2:
            continue  # a rate needs two points — a lone sample contributes 0
        curr = samples[0]
        newest_ts = max(newest_ts, curr[1])
        # ── flush-aware staircase rate, shared by tokens AND dollars ──
        # Counters only move when a bot flushes usage (a completed message,
        # or for the Discord gpt bot a whole completed TURN), so both
        # cumulative series are staircases: long flat plateaus, then one jump
        # carrying minutes of work. The honest rate for a jump divides it by
        # the turn that produced it — the span the counter sat still — with a
        # windowed average for chatty bots whose flushes land closer together
        # than RATE_WINDOW_SEC. After the last flush the reading holds
        # through RATE_GRACE_SEC and then fades to zero (RATE_FADE_SEC), so
        # both instruments wind down instead of freezing.
        #
        # Dollars get their own walk rather than riding the token anchors:
        # cache reads bill money while adding no WORK tokens, so the cost
        # staircase can step where the token one stays flat. Same estimator,
        # same decay, two independent staircases — the needle and the $/hr
        # readout behave identically, which is the point (reported: "est $/hr
        # decay weird and i dont rly know whats going on").
        def staircase_rate(idx: int, cap: Optional[float] = None) -> float:
            cur_start = curr          # where the current value landed
            prev_val = None           # the value before it
            plateau_start = None      # where THAT value landed
            for s in samples[1:]:
                if prev_val is None:
                    if s[idx] == curr[idx]:
                        cur_start = s
                    else:
                        prev_val = s[idx]
                        plateau_start = s
                elif s[idx] == prev_val:
                    plateau_start = s
                else:
                    break
            if plateau_start is None:
                return 0.0  # counter flat across all retained history
            turn_span = cur_start[1] - plateau_start[1]
            if turn_span < RATE_WINDOW_SEC:
                base = next((s for s in samples[1:]
                             if curr[1] - s[1] >= RATE_WINDOW_SEC), samples[-1])
                span = curr[1] - base[1]
                jump = _delta(curr[idx], base[idx])
            else:
                span = turn_span
                jump = _delta(curr[idx], prev_val)
            if span <= 0:
                return 0.0
            # sanity governor: a jump implying an impossible per-bot rate is a
            # counter anomaly (midnight source-replay), not work — read zero
            if cap is not None and jump / span > cap:
                return 0.0
            quiet = now - cur_start[1]
            fade = min(1.0, max(0.0, 1.0 - (quiet - RATE_GRACE_SEC) / RATE_FADE_SEC))
            return (jump / span) * fade

        tok_s = staircase_rate(3, MAX_SANE_BOT_TOK_S)
        usd_h = staircase_rate(4, MAX_SANE_BOT_USD_S) * 3600.0
        host = curr[2] or ""
        # When this bot's token counter last MOVED, scanned over the whole
        # retained history rather than the UI's selected window. The roster
        # needs a window-independent answer to "how long has this been quiet"
        # — reading it from the sparkline payload meant a bot quiet longer
        # than the selected range fell out of the data entirely and couldn't
        # be distinguished from one that stopped a minute ago (the user
        # 2026-08-01, "idle >2h should say inactive"). None = no movement
        # anywhere in retention, which is itself the strongest quiet signal.
        last_burn_ts = last_burn.get(name)
        bots.append({
            "name": name,
            "host": host,
            # Every session is scanned from local disk on the same tick, so
            # there is no coarse/live split to signal. The field stays because
            # the panel reads it; it is simply always true here.
            "live": True,
            "tok_per_sec": tok_s,
            "usd_per_hour": usd_h,
            "last_burn": last_burn_ts,
            # When this bot's scan last ARRIVED, which is a different question
            # from when it last burned tokens. The sampler writes a row every
            # tick whether or not the counters moved, so the newest sample's
            # timestamp answers "is the link alive" on its own.
            #
            # The LOST lamp needs exactly this. Driven off last_burn it could
            # not tell a dead ssh link from a bot that is simply quiet, so an
            # idle-but-healthy a remote session lit LOST (reported). Two
            # threshold tweaks failed to fix that because the threshold was
            # never the problem — the signal was.
            "last_scan": samples[0][1] if samples else None,
            # how deep the retained history actually is for this bot, so the
            # UI knows whether "never moved" is provable or just unobserved
            "history_sec": (now - samples[-1][1]) if samples else 0.0,
            # Today's settled totals for THIS bot. The fleet figures below are
            # the sum of exactly these values, but only the sum was ever
            # published — so "what did the fleet spend today" was answerable
            # and "which bot spent it" was not, despite both numbers sitting
            # in the same loop. Cumulative counters are Pacific-midnight
            # bucketed, so the newest sample IS the day's total.
            "tokens_today": (samples[0][3] or 0) if samples else 0,
            "usd_today": (samples[0][4] or 0) if samples else 0.0,
        })
        fleet_tok += tok_s
        fleet_usd += usd_h

    stale = bool(newest_ts) and (now - newest_ts) > STALE_AFTER_SEC
    if stale or not newest_ts:
        fleet_tok = fleet_usd = 0.0
        for b in bots:
            b["tok_per_sec"] = 0.0
            b["usd_per_hour"] = 0.0

    # Fleet work-token odometer: the cumulative counters are TODAY-bucketed
    # (they reset at Pacific midnight), so the newest sample per bot summed
    # is the fleet's work tokens today — the dial's odometer line.
    #
    # Summed from the PER-BOT values published above rather than re-derived
    # from per_bot: same numbers either way, but re-deriving let the roster and
    # the odometer drift apart silently if one side's source ever changed. The
    # fleet total is now the sum of the rows, by construction.
    tokens_today = sum(b["tokens_today"] for b in bots)
    # Same bucketing for the money column — dollars spent today, which unlike
    # usd_per_hour is a settled total rather than a decaying rate.
    usd_today = sum(b["usd_today"] for b in bots)
    bots.sort(key=lambda b: -b["tok_per_sec"])
    result = {
        "fleet": {
            "tok_per_sec": fleet_tok,
            "tok_per_min": fleet_tok * 60.0,
            "tok_per_hour": fleet_tok * 3600.0,
            "usd_per_hour": fleet_usd,
            "tokens_today": tokens_today,
            "usd_today": usd_today,
        },
        "bots": bots,
        "remote_gap_sec": _remote_gap[0],
        "stale": stale,
        "ts": newest_ts or None,
    }
    if live:
        _rate_cache[0] = (time.time(), result)
    return result


def _paint_fade(sample_ts: float, plateau_start: float) -> float:
    """How much of a flush's rate a sample inside its plateau should carry.

    Back-painting a jump across the run that produced it is what stops a minute
    of generation reading as one 5s spike then zeros. But it had no bound: a bot
    that flushed, went quiet for half an hour, then flushed again painted that
    ENTIRE silence at the second flush's rate. Overnight, five bots each holding
    a long pending plateau summed to a permanent ~2 tok/s floor on a fleet that
    was doing nothing (reported: "stuck on 3 tok/s even tho nobody was
    home").

    current_rate() has always wound down — full value through RATE_GRACE_SEC,
    then a linear fade to zero over RATE_FADE_SEC. The sparkline now uses the
    same curve, measured from where the plateau began, so both instruments tell
    the same story about the same silence. Work still paints at full rate: a
    flush lands within grace of its own turn, so sustained bots are untouched.
    """
    quiet = sample_ts - plateau_start
    if quiet <= RATE_GRACE_SEC:
        return 1.0
    return min(1.0, max(0.0, 1.0 - (quiet - RATE_GRACE_SEC) / RATE_FADE_SEC))


def _exact_window_totals(*, cutoff: float, now: float
                         ) -> tuple[float, float, dict[str, dict[str, float]]]:
    """Exact completed work and cost in the half-open window ``(cutoff, now]``.

    The rate curve intentionally back-paints completed flushes over the quiet
    plateau that preceded them, fades that paint after the grace period, and
    bucket-max downsamples long responses. Those are display operations and
    therefore cannot be integrated back into accounting totals.

    The sample at or immediately before ``cutoff`` is only a baseline. Deltas
    are booked when the *new* cumulative observation lands after the cutoff.
    This makes adjacent windows disjoint: a flush observed exactly on their
    shared boundary belongs to the earlier window and cannot be counted twice.
    Counter decreases remain resets/prunes and contribute zero, matching
    ``_delta`` everywhere else in this module.
    """
    conn = _connect()
    try:
        baselines = conn.execute(
            "SELECT s.bot, s.ts, s.tokens, s.cost FROM samples s "
            "JOIN (SELECT bot, MAX(ts) AS ts FROM samples "
            "      WHERE ts <= ? GROUP BY bot) b "
            "ON s.bot = b.bot AND s.ts = b.ts",
            (cutoff,),
        ).fetchall()
        inside = conn.execute(
            "SELECT bot, ts, tokens, cost FROM samples "
            "WHERE ts > ? AND ts <= ? ORDER BY bot ASC, ts ASC",
            (cutoff, now),
        ).fetchall()
    finally:
        conn.close()

    rows = sorted([*baselines, *inside], key=lambda r: (r[0], r[1]))
    state: dict[str, tuple[float | None, float | None]] = {}
    totals: dict[str, dict[str, float]] = {}
    for bot, ts, tokens, cost in rows:
        prev = state.get(bot)
        state[bot] = (tokens, cost)
        if prev is None or ts <= cutoff:
            continue
        item = totals.setdefault(bot, {"total_tokens": 0.0, "cost_total": 0.0})
        item["total_tokens"] += _delta(tokens, prev[0])
        item["cost_total"] += _delta(cost, prev[1])

    work = sum(v["total_tokens"] for v in totals.values())
    cost = sum(v["cost_total"] for v in totals.values())
    return work, cost, totals


def history(*, since_sec: float = 3600.0, now: float | None = None
            ) -> dict[str, Any]:
    """Fleet-wide rate series for the dial's sparkline.

    Reads the staircases the way current_rate() does: a jump is spread back
    across the flat run that preceded it (the turn that produced it), not
    divided by the one sample gap it happened to land in. Adjacent-pair maths
    put a 1.4M-token flush into a single ~40s interval — a 36k tok/s spike on
    the sparkline while the dial showed the honest ~1.9k sustained, and the
    two instruments seemed to disagree (reported). Totals are
    preserved (rate x span = delta); an unfinished flat run contributes zero
    until its flush arrives."""
    now = time.time() if now is None else now
    cutoff = now - since_sec
    exact_work, exact_cost, exact_bots = _exact_window_totals(
        cutoff=cutoff, now=now)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bot, ts, tokens, cost FROM samples "
            "WHERE ts >= ? AND ts <= ? ORDER BY bot ASC, ts ASC",
            (cutoff, now),
        ).fetchall()
    finally:
        conn.close()

    buckets: dict[float, dict[str, float]] = {}
    state: dict[str, dict] = {}
    for bot, ts, tokens, cost in rows:
        st = state.get(bot)
        if st is None:
            state[bot] = {"prev_ts": ts, "tok": tokens, "tok_ts": ts,
                          "tok_pend": [], "usd": cost, "usd_ts": ts,
                          "usd_pend": [], "total_tok": 0.0,
                          "last_rate": 0.0, "last_active": None}
            continue
        if ts <= st["prev_ts"]:
            continue
        st["prev_ts"] = ts
        buckets.setdefault(ts, {"tok": 0.0, "usd": 0.0,
                                "tok_codex": 0.0, "usd_codex": 0.0})
        # Which vendor's meter this bot spins. The split is accumulated
        # alongside the fleet total rather than derived later: the staircase
        # paints a jump back across the run that produced it, so a bot's
        # contribution is only knowable here, inside its own state machine.
        vend = "_codex" if bot in CODEX_BOTS else ""
        # token staircase: buckets accumulate while the counter sits flat;
        # when the jump lands, its sustained rate paints the whole run
        st["tok_pend"].append(ts)
        if tokens != st["tok"]:
            span = ts - st["tok_ts"]
            jump = _delta(tokens, st["tok"])
            # sanity governor (see MAX_SANE_BOT_TOK_S): an impossible rate is
            # the midnight source-replay, not work — re-anchor, paint nothing,
            # and keep it out of the roster totals too (the replay would
            # otherwise double a bot's day in its TOTAL column)
            if span > 0 and jump / span <= MAX_SANE_BOT_TOK_S:
                r = jump / span
                for t in st["tok_pend"]:
                    painted = r * _paint_fade(t, st["tok_ts"])
                    buckets[t]["tok"] += painted
                    if vend:
                        buckets[t]["tok" + vend] += painted
                # per-bot bookkeeping for the roster: what this bot burned
                # inside the window, and its final sustained rate — served so
                # "recent activity" survives page reloads instead of living
                # only in client memory (reported).
                st["total_tok"] += jump
                st["last_rate"] = r
                st["last_active"] = ts
            st["tok"] = tokens
            st["tok_ts"] = ts
            st["tok_pend"] = []
        # cost staircase, tracked independently — cache reads move dollars
        # on turns that add no work tokens
        st["usd_pend"].append(ts)
        if cost != st["usd"]:
            span = ts - st["usd_ts"]
            if span > 0 and _delta(cost, st["usd"]) / span <= MAX_SANE_BOT_USD_S:
                r = (_delta(cost, st["usd"]) / span) * 3600.0
                for t in st["usd_pend"]:
                    painted = r * _paint_fade(t, st["usd_ts"])
                    buckets[t]["usd"] += painted
                    if vend:
                        buckets[t]["usd" + vend] += painted
            st["usd"] = cost
            st["usd_ts"] = ts
            st["usd_pend"] = []

    ordered = sorted(buckets.items())
    ts_out = [t for t, _ in ordered]
    tok_out = [v["tok"] for _, v in ordered]
    usd_out = [v["usd"] for _, v in ordered]
    tokx_out = [v["tok_codex"] for _, v in ordered]
    usdx_out = [v["usd_codex"] for _, v in ordered]
    # Downsample long windows to ~720 points via bucket-MAX (a spike must not
    # be averaged away): 24h at the 5s cadence is ~17k samples, which is dead
    # weight over the wire when the canvas is ~640px wide.
    if len(ts_out) > 720:
        step = -(-len(ts_out) // 720)
        ts_out2, tok_out2, usd_out2 = [], [], []
        tokx_out2, usdx_out2 = [], []
        for i in range(0, len(ts_out), step):
            ts_out2.append(ts_out[min(i + step - 1, len(ts_out) - 1)])
            tok_out2.append(max(tok_out[i:i + step]))
            usd_out2.append(max(usd_out[i:i + step]))
            # The codex band is downsampled by the SAME slice, not by its own
            # max: bucket-MAX on each series independently would let the
            # sub-series exceed the total in a slice where the two peak on
            # different samples, and a stacked band taller than its own total
            # is nonsense. Take the codex value AT the slice's total-max
            # instead, so the pair stays internally consistent.
            sl = slice(i, i + step)
            j = i + max(range(len(tok_out[sl])), key=lambda k: tok_out[sl][k])
            tokx_out2.append(tokx_out[j])
            k2 = i + max(range(len(usd_out[sl])), key=lambda k: usd_out[sl][k])
            usdx_out2.append(usdx_out[k2])
        ts_out, tok_out, usd_out = ts_out2, tok_out2, usd_out2
        tokx_out, usdx_out = tokx_out2, usdx_out2
    # ROUNDED on the wire. Full float precision costs 49% of this payload —
    # measured 32.2 KB -> 16.4 KB on a today window — to carry ~15 significant
    # digits into a canvas about 640px wide. Worst-case rendering difference
    # from 3 decimals: 0.0006px. Timestamps go to whole seconds; the sampler
    # ticks every 3-5s, so sub-second precision was never real either. This is
    # the single biggest thing on the wire (81% of all API bytes at 30 KB a
    # poll, every 10s), so it is worth the two list comprehensions.
    bot_names = set(state) | set(exact_bots)
    return {
        "ts": [round(t) for t in ts_out],
        "tok_per_sec": [round(v, 3) for v in tok_out],
        "usd_per_hour": [round(v, 3) for v in usd_out],
        # Vendor split — the CODEX share of each bucket, on the same indices
        # as the totals above. Only one side is sent: Claude is the total
        # minus this, so shipping both would be a redundant array that could
        # disagree with its own sum after rounding.
        "tok_per_sec_codex": [round(v, 3) for v in tokx_out],
        "usd_per_hour_codex": [round(v, 3) for v in usdx_out],
        # Exact accounting fields. These are raw cumulative-counter deltas in
        # (cutoff, now], calculated before smoothing or downsampling.
        "work_tokens_total": exact_work,
        "cost_total": exact_cost,
        "retention_sec": RETENTION_SEC,
        # per-bot window aggregates for the roster's TOTAL column + reload-
        # surviving recent state
        "bots": {
            name: {
                "total_tokens": (exact_bots.get(name) or {}).get(
                    "total_tokens", 0.0),
                "cost_total": (exact_bots.get(name) or {}).get(
                    "cost_total", 0.0),
                "last_active": (state.get(name) or {}).get("last_active"),
                "last_rate": (state.get(name) or {}).get("last_rate", 0.0),
            }
            for name in bot_names
            if ((exact_bots.get(name) or {}).get("total_tokens", 0.0) > 0
                or (exact_bots.get(name) or {}).get("cost_total", 0.0) > 0)
        },
        # the EFFECTIVE window (callers clamp requests to retention) so the
        # UI can label the panel with what it is actually showing
        "window_sec": since_sec,
    }


def prune_now(*, now: float | None = None) -> int:
    """Drop samples past RETENTION_SEC. Returns rows removed."""
    now = time.time() if now is None else now
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM samples WHERE ts < ?",
                           (now - RETENTION_SEC,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ─────────────────────────── sampler thread ───────────────────────────

_sampler_started = False
_last_prune = 0.0


def _scan_sessions() -> list[dict[str, Any]]:
    """One snapshot of every Claude Code session, from the transcripts on disk.

    This is the single-host replacement for the multi-machine ssh sweep the
    panel was carved out of. There is no agent registry to consult: a session
    IS a transcript file, so the roster is whatever Claude Code has written.

    Sessions are grouped by their project directory, because that is the unit
    a person actually thinks in ("what is the API repo costing me"), and a
    single project's transcripts are one continuous piece of work even when
    they span several files.
    """
    from . import claude_usage, config

    projects = config.projects_dir()
    if not os.path.isdir(projects):
        return []

    rows: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(projects), key=lambda e: e.name)
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        res = claude_usage.compute(entry.path)
        if not res["turns"]:
            continue
        total = res["total"]
        # Claude Code names a project directory after its working path with
        # the separators escaped ("-home-alice-src-api"). The last segment is
        # what a person calls the project, but on its own it collides — two
        # checkouts both ending in "api" would merge into one row and silently
        # sum each other's burn. Keep the parent as a qualifier when there is
        # one, which disambiguates without printing the whole path.
        parts = [p for p in entry.name.split("-") if p]
        name = "/".join(parts[-2:]) if len(parts) >= 2 else (
            parts[-1] if parts else entry.name)
        rows.append({
            "name": name,
            "host": "local",
            "out_today": total["output"],
            "cost_counter": total["cost"],
            # `in` is FRESH input only — cache lanes are context size, not
            # work, and counting them makes a long conversation look busy
            # while it sits idle.
            "detail": {"in": total["input"], "kind": "claude"},
        })
    return rows


def start_sampler() -> None:
    """Background daemon that snapshots the transcripts on a fixed cadence.

    Adaptive: ACTIVE_INTERVAL_SEC while anything is burning, decaying to
    IDLE_INTERVAL_SEC after IDLE_AFTER_SEC of quiet, so the needle tracks real
    work without re-reading every transcript around the clock.

    A bad tick must never kill the loop — this thread is the only writer, and
    a dead sampler shows up as a gauge frozen at its last reading, which is
    worse than a gap because it looks like data.
    """
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True

    def _prewarm() -> None:
        """Pay the cold-scan cost at boot so the first visitor doesn't.

        The transcript scan is measured in seconds on a cold page cache
        against milliseconds once warm, and nothing renders until it lands —
        so a freshly started server showed a blank panel for long enough to
        read as broken rather than slow. The work happens either way; this
        moves it off the request path, where a human is waiting, into startup.
        """
        try:
            record(_scan_sessions())
            current_rate()
        except Exception:
            pass

    threading.Thread(target=_prewarm, name="tachometer-prewarm",
                     daemon=True).start()

    def loop() -> None:
        global _last_prune
        last_busy = 0.0
        last_total = None
        interval = IDLE_INTERVAL_SEC
        while True:
            tick_start = time.time()
            try:
                rows = _scan_sessions()
                record(rows)

                # Cadence follows the WORK, not the clock: sum the counters and
                # speed up only when the total actually moves. Polling fast
                # while idle costs a full transcript re-read every few seconds
                # and buys nothing, since the needle would be tracing a flat
                # line either way.
                total = sum(float(r.get("out_today") or 0) for r in rows)
                now = time.time()
                if last_total is not None and total > last_total:
                    last_busy = now
                last_total = total
                interval = (ACTIVE_INTERVAL_SEC
                            if now - last_busy < IDLE_AFTER_SEC
                            else IDLE_INTERVAL_SEC)

                # Retention trim, at most hourly. The store is a rolling window
                # for the sparkline; the ledger owns the long horizon.
                if now - _last_prune > 3600:
                    _last_prune = now
                    try:
                        prune_now()
                    except Exception:
                        pass
            except Exception:
                # Swallowed on purpose — see the docstring. Back off to the
                # idle cadence so a persistent failure does not spin.
                interval = IDLE_INTERVAL_SEC

            spent = time.time() - tick_start
            time.sleep(max(interval - spent, 0.5))

    threading.Thread(target=loop, name="tachometer-sampler",
                     daemon=True).start()
