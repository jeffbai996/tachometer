# tachometer

A live instrument panel for what Claude Code is costing you.

Point it at your Claude Code config directory and it reads the transcripts
already on disk. No proxy, no API key, no instrumentation, nothing to sign up
for. If you use Claude Code, the input already exists.

```bash
git clone https://github.com/jeffbai996/tachometer
cd tachometer
python -m venv venv && venv/bin/pip install -e .
venv/bin/tachometer          # -> http://localhost:8710
```

## Why an instrument and not a table of numbers

A number tells you what happened. An instrument tells you what is happening.

A dial you glance at answers "is something running away right now" faster than
a figure you have to read, then compare against a figure you remember. That is
the whole argument: the gauge, the lamps, and the burn curve are the product,
and the numbers underneath are available anywhere.

## What it shows

**The dial** — live token velocity across every session it can see, with a
redline and a reserve arc for what is left of your weekly quota. The needle has
real ballistics and a decay curve, so a burst reads as a burst and a stopped
session winds down instead of dropping dead to zero.

**The annunciators** — a row of lamps for conditions worth knowing without
reading anything: a session compacting, a spike well above the running average,
a burn rate past your threshold, a link that stopped answering. They warm and
cool like real panel lamps rather than switching, so movement catches the eye at
the edge of vision. Every lamp names its own threshold on hover.

**The roster** — each session with its state, tokens today, and current rate, so
a runaway is attributable rather than merely visible. Hovering a row reveals its
model and context size.

**The graph** — token velocity, dollar burn, cumulative spend, or daily spend,
over windows from an hour to 90 days. Hovering names the sample under the
cursor. `MIX` splits the curve into stacked bands by vendor, which answers
"which subscription is this" without a second chart.

## Configuration

Written to `~/.config/tachometer/config.toml` on first run:

```toml
# Where to look. Claude Code's own config directory; transcripts live in
# <config_dir>/projects. CLAUDE_CONFIG_DIR overrides this if it is set.
config_dir = "~/.claude"

# USD per million tokens, [input, output]. These are published list prices and
# they go stale — edit freely, and add your own tiers. Matched against the
# model id by substring, longest key first, so "opus" covers every Opus
# release while an "opus-4-8" entry would override just that one.
[rates]
opus   = [5.0, 25.0]
sonnet = [3.0, 15.0]
haiku  = [1.0, 5.0]

# Cache reads bill at a fraction of the input rate; writes at a premium.
cache_read_multiplier  = 0.10
cache_write_multiplier = 1.25
```

A model with no rate is counted at **$0** and named by `--doctor`, rather than
being quietly folded into the total at a guessed price.

The dollar figures are a **local estimate** from token counts and the rates
above, the same way Claude Code's own `/usage` computes it. On a subscription
they are not a bill — they are what the same work would have cost through the
API, which is the number that says whether a workflow was worth its tokens.

## Requirements

Python 3.11+ and Flask. That is the list. The panel is a single HTML file with
no build step and no bundler.

## Troubleshooting

```bash
tachometer --doctor
```

Prints the config directory it resolved, how many transcripts and turns it
found, the estimated cost, and every model it saw — flagging any that have no
rate configured. Most "it shows nothing" reports are a config directory
somewhere non-default, and this says so in one line.

## Regenerating the screenshot

```bash
tachometer &
python docs/shoot_hero.py
```

Requires `playwright` and `playwright install chromium`.

## License

MIT.
