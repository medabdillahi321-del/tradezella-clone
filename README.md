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
| `config/sessions.toml` | Daily windows in local time; each marked `approved` or `provisional` |
| `scripts/check_env.py` | Milestone 0 readiness check |
| `tests/` | Tests, including both daylight-saving changes in the US and the UK |
| `PROJECT_STATUS.md` | Where the project stands and what needs your approval |
