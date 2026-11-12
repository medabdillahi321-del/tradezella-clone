# FX shadow trading system

A system that watches EUR/USD through OANDA's practice API, applies fixed risk rules, and records
virtual trades for review. No strategy is included yet, and **nothing in this project places
orders**: the OANDA client only allows read requests to the practice servers.

Needs Python 3.11 or newer and nothing else (no `pip install`).
On Windows, if the time-zone check fails, run `python -m pip install tzdata` once.

## Check your computer

```
python scripts/check_env.py
```

Once you have an OANDA practice account and API token, set the token as the environment variable
`OANDA_TOKEN` (never put it in a file in this folder or paste it into a chat), then run:

```
python scripts/check_env.py --online
```

## Run the tests

```
python -m unittest discover -s tests -t .
```

## Layout

| Path | What it is |
|---|---|
| `fxsystem/clock.py` | Time conversion, sessions, trading day (17:00 New York), market hours |
| `fxsystem/oanda_read.py` | Read-only OANDA client: practice hosts, GET requests and read endpoints only |
| `fxsystem/storage.py` | Local event log (SQLite): written first, append-only, hash-chained, no duplicates |
| `fxsystem/sync.py` | Copies the local log to Supabase; safe to retry, loses nothing while Supabase is down |
| `supabase/migrations/001_events.sql` | The matching Supabase table, append-only. Not applied yet |
| `fxsystem/recorder.py` | Records every price to daily files and builds 1-minute bid/ask bars |
| `scripts/record.py` | Run the recorder (`--minutes N` to stop after N minutes) |
| `scripts/compare_bars.py` | Compare recorded bars with OANDA's own 1-minute candles |
| `fxsystem/quality.py` | Live NO TRADE gate for each price, and the end-of-day data audit |
| `scripts/check_quality.py` | Print and log the data quality report for a trading day |
| `config/quality.toml` | Data-quality limits (all provisional) |
| `fxsystem/dukascopy.py` | Downloads and decodes Dukascopy hourly tick files; slow, cached, stops when refused |
| `fxsystem/crosscheck.py` | Compares two price sources minute by minute (mid price, spreads side by side) |
| `scripts/crosscheck.py` | OANDA vs Dukascopy for a day's operating window or any UTC range |
| `fxsystem/history.py` | Downloads OANDA 1-minute bid/ask history into daily files, with checksums |
| `fxsystem/replay.py` | Replay runner, the strategy slot, and the placeholder "plumbing-test" strategy |
| `scripts/load_history.py` | Download history for a range of trading days |
| `scripts/replay.py` | Replay saved history; same input always gives the same fingerprint |
| `fxsystem/risk.py` | Risk engine: sizing, daily limits, every NO TRADE guard; fails closed |
| `scripts/kill.py` | Kill switch: `on` stops all new trades, `off` allows them again |
| `config/risk.toml` | Risk limits, each marked approved, default or provisional |
| `fxsystem/shadow.py` | Shadow simulator: virtual account, fills at real bid/ask, partials, conservative rules |
| `scripts/shadow_replay.py` | History through strategy, data check, risk engine and simulator |
| `fxsystem/qt_export.py` | Exports a shadow run for Quant Terminal: bars, trade markers, signal, trade list |
| `scripts/export_quant_terminal.py` | Export the latest (or a chosen) shadow run; see HOW_TO_OPEN.txt in the output |
| `scripts/check_qt_indicator.mjs` | Checks a generated indicator file with Node.js, without running Quant Terminal |
| `config/sessions.toml` | Daily windows in local time; each marked `approved` or `provisional` |
| `scripts/check_env.py` | Milestone 0 readiness check |
| `tests/` | Tests, including both daylight-saving changes in the US and the UK |
| `PROJECT_STATUS.md` | Where the project stands and what needs your approval |
