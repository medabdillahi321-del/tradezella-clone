# Project status

## Decisions made
- Data and later demo trading: OANDA (practice environment only).
- Instrument: EUR/USD. Operating window: 09:00-12:00 America/New_York.
- Strategy: not part of this phase. A placeholder strategy will be used to test the plumbing.
- Risk settings to build (not yet built): 0.25% per trade, at most 4 new trades per trading day,
  1.00% daily equity-loss shutdown.

## Milestones
| # | Milestone | Status |
|---|---|---|
| 0 | Computer and OANDA check | Done. Practice API connected and EUR/USD prices read |
| 1 | Clock and sessions | Done |
| 2 | Storage (local log first, then Supabase) | Done locally. Supabase table written, not applied |
| 3 | OANDA price recorder | Done. Live test: complete bars within 0.1 pip of OANDA candles |
| 4 | Data quality checker | Done. Live NO TRADE gate and end-of-day audit; real recording audited clean |
| 5 | Cross-check feed (Dukascopy) | Code done and tested on sample data. Not yet run on real Dukascopy data: it refuses this cloud network. Run `python scripts/crosscheck.py --day <date>` on your own computer |
| 6 | History loader and replay runner | Not started |
| 7 | Risk engine | Not started |
| 8 | Shadow simulator | Not started |
| 9 | Quant Terminal export | Not started |
| 10 | Journal importer | Not started |
| 11 | Health monitor and kill switch | Not started |
| 12 | AI review, Claude audits, demo execution | Not started; needs approved strategy rules |

## Needs your approval
- **Trading day boundary: 17:00 New York** (the forex convention). The 4-trade cap and the 1% daily
  loss limit reset at this time. Built as the default; confirm or change it.
- **Asia, London and New York session times** in `config/sessions.toml` are placeholders marked
  `provisional`, not source-defined values.
- **Four trades at 0.25% equals the 1.00% daily limit.** Four losses plus costs reach the shutdown
  exactly. Confirm this is intended.

- **Supabase project:** the free plan should be enough to start. Applying
  `supabase/migrations/001_events.sql` needs your go-ahead.

- **Data-quality limits** in `config/quality.toml` are provisional starting points: stale after 10 s,
  frozen after 120 s, spread above 3.0 pips, spike above 15 pips (then 60 s pause), price outside
  0.50-2.00. Review them once you have a few weeks of recordings.

## Open checks
- Dukascopy blocks requests from the cloud computer (connection reset, HTTP 429). Expected to work
  from your own computer. The file layout the code assumes is from memory and is confirmed only
  once a real file has been decoded.
- Dukascopy's terms of use for its free historical data have not been read yet.
- OANDA price stream (stream-fxpractice.oanda.com) needs its own key entry before the recorder can run in the cloud.
- Whether a separate OANDA token can be limited to one account, for the later execution service.
