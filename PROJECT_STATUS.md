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
| 1 | Clock and sessions | Done, 22 tests passing |
| 2 | Storage (local log first, then Supabase) | Not started |
| 3 | OANDA price recorder | Not started (read-only client and its 6 tests done) |
| 4 | Data quality checker | Not started |
| 5 | Cross-check feed (Dukascopy or a second broker) | Not started |
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

## Open checks
- OANDA price stream (stream-fxpractice.oanda.com) needs its own key entry before the recorder can run in the cloud.
- Whether a separate OANDA token can be limited to one account, for the later execution service.
