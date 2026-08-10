# tachometer

A live instrument panel for what Claude Code is costing you.

![The tachometer panel](docs/hero.png)

Point it at your Claude Code config directory and it reads the transcripts
already on disk. No proxy, no API key, no instrumentation, nothing to sign up
for. If you use Claude Code, the input already exists.

```bash
pip install tachometer
tachometer            # -> http://localhost:8710
```

## Why this and not a table of numbers

Because a number tells you what happened and an instrument tells you what is
happening. A dial you glance at answers "is something running away right now"
faster than a figure you have to read and compare against a figure you
remember. The gauge, the annunciators, and the burn curve are the product; the
numbers underneath are available anywhere.

## What it shows

**The dial** is live token velocity across every agent it can see, with a
redline and a reserve arc for your remaining budget. The needle has real
ballistics, so a burst reads as a burst.

**The annunciator cluster** is a row of lamps for conditions worth knowing
without reading anything: a session compacting, a spike well above the running
average, a burn rate past your threshold, a datalink that stopped answering.
They light like panel lamps rather than switching, so movement catches the eye
at the edge of vision.

**The roster** lists each agent with its state, tokens today, and current rate,
so a runaway session is attributable rather than just visible.

**The graph** plots token velocity, dollar burn, cumulative spend, or daily
spend over windows from an hour to 90 days. Hovering names the sample. Turning
on `MIX` splits the curve by vendor as stacked bands, which answers "which
subscription is this" without a second chart.

## Configuration

Everything lives in `~/.config/tachometer/config.toml`, written on first run:

```toml
# Where to look. The default is Claude Code's own config directory.
config_dir = "~/.claude"

# Per-model rates in USD per million tokens, [input, output].
# These are published list prices and they go stale — edit freely, and add
# your own tiers.
[rates]
opus   = [5.0, 25.0]
sonnet = [3.0, 15.0]
haiku  = [1.0, 5.0]

# Cache reads bill at a fraction of input; cache writes at a premium.
cache_read_multiplier  = 0.10
cache_write_multiplier = 1.25
```

The dollar figures are a **local estimate** computed from token counts and the
rates above, the same way Claude Code's own `/usage` does it. On a subscription
they are not a bill — they are what the same work would have cost through the
API, which is the number that tells you whether a workflow is worth its tokens.

## Requirements

Python 3.11+. That is the whole list — the panel is one HTML file with no build
step, no bundler, and no runtime dependencies beyond Flask.

## Troubleshooting

```bash
tachometer --doctor
```

Prints the config directory it resolved, how many transcripts it found, the
date range they cover, and the models it recognised. Most "it shows nothing"
reports are a config directory somewhere non-default, and this says so in one
line.

## License

MIT.
