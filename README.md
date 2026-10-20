# tachometer

A live instrument panel for Claude Code token velocity and estimated cost. Reads the transcripts already on disk; no proxy or API key.

## Install

```bash
git clone https://github.com/jeffbai996/tachometer
cd tachometer
python -m venv venv && venv/bin/pip install -e .
```

Python 3.11+, Flask.

## Usage

```bash
tachometer [--host 127.0.0.1] [--port 8710] [--doctor]
```

Serves the panel at http://localhost:8710. `--doctor` prints the resolved config directory, transcript and turn counts, estimated cost, and every model seen, flagging models with no rate. If the panel shows nothing, run it first: the usual cause is a non-default config directory.

Panel: dial (token velocity, redline), annunciator lamps (hover for thresholds), session roster, and a graph (velocity, burn, cumulative, daily; 1 hour to 90 days; `MIX` splits by vendor).

## Configuration

`~/.config/tachometer/config.toml`, written on first run. Defaults and comments are in `tachometer/config.py`.

- `config_dir`: Claude Code config directory; transcripts are read from `<config_dir>/projects`. `CLAUDE_CONFIG_DIR` overrides it.
- `[rates]`: USD per million tokens, `[input, output]`, matched to the model id by substring, longest key first.
- `cache_read_multiplier`, `cache_write_multiplier`: cache billing relative to the input rate.

State: `$XDG_DATA_HOME/tachometer/` (default `~/.local/share/tachometer/`; `TACHOMETER_DATA_DIR` moves the usage ledger). Holds `token_rate.db` (sample cache) and `usage_ledger.json`.

## Notes

- A model with no configured rate is counted at $0 and named by `--doctor`.
- Dollar figures are a local estimate from token counts and the configured rates. On a subscription they are not a bill.
- The reserve arcs and remote-link lamps need quota and remote-host data this server does not collect; they stay idle.

## Tests

```bash
pip install pytest
python -m pytest tests
```

## Regenerating the screenshot

```bash
tachometer &
python docs/shoot_hero.py
```

Needs `playwright` and `playwright install chromium`. Writes `docs/hero.png`, which is not committed.

## License

MIT.
