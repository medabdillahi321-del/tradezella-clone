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
| 5 | Cross-check feed (Dukascopy) | Done. Real check, 11 Nov 09:00-10:00 New York: 60 of 60 minutes, mid prices within 0.25 pip, no minutes flagged |
| 6 | History loader and replay runner | Done. 4 weeks of real OANDA history (31,508 bars) replayed twice with identical results |
| 7 | Risk engine | Done. 0.25% sizing, 4-trade cap, 1% daily shutdown, worst-case check, 20+ NO TRADE guards, kill switch |
| 8 | Shadow simulator | Done. Full pipeline on 4 weeks of real history: daily cap and 1% limit held, runs repeatable |
| 9 | Quant Terminal export | Done. Bars, trade markers, signal and trade list; indicator checked in Node.js. Still to open in Quant Terminal on your laptop |
| 10 | Journal importer | Not started |
| 11 | Health monitor and kill switch | Not started |
| 12 | AI review, Claude audits, demo execution | Not started; needs approved strategy rules |

## Needs your approval
- **Trading day boundary: 17:00 New York** (the forex convention). The 4-trade cap and the 1% daily
  loss limit reset at this time. Built as the default; confirm or change it.
- **Asia, London and New York session times** in `config/sessions.toml` are placeholders marked
  `provisional`, not source-defined values.
- **Four trades at 0.25% equals the 1.00% daily limit.** Built with a cap of 4 (setting in
  `config/risk.toml`). In practice the worst-case check refuses a new trade whenever it could push
  the day past 1%, so after three full losses (about $75 on $10,000 plus costs) a fourth full-size
  trade is refused. Confirm 4, or choose 3.
- **Risk settings marked provisional** in `config/risk.toml`: sizing on the lower of start-of-day
  and current equity, 0.5-pip slippage allowance, stops between 3 and 50 pips, at most half the
  available margin, account data at most 30 s old.

- **Supabase project:** the free plan should be enough to start. Applying
  `supabase/migrations/001_events.sql` needs your go-ahead.

- **Data-quality limits** in `config/quality.toml` are provisional starting points: stale after 10 s,
  frozen after 120 s, spread above 3.0 pips, spike above 15 pips (then 60 s pause), price outside
  0.50-2.00. Review them once you have a few weeks of recordings.

## Findings from 4 weeks of OANDA history (13 Oct - 11 Nov 2026)
- Inside 09:00-12:00 New York: every minute present (3,960 = 22 days x 180), median spread 1.6 pips,
  95% of minutes at 1.7 or below, widest 2.6. Never above the 3.0-pip limit.
- Wide spreads cluster at the 17:00 New York rollover (707 of 714 bars), outside the window.

- **Close trades by a set time?** Without it, virtual trades can run past 12:00 and through the
  17:00 New York rollover (widest spreads) into the next day. Option built (`--flat-at 12:00`),
  off by default. Your choice, ideally from the strategy rules.

## Open checks
- Dukascopy refuses the cloud computer and timed out once for the script on your laptop, while
  your browser downloads fine. Fallback: `python scripts/crosscheck.py --day <date> --list-files`
  lists the files to download by browser. File layout confirmed on a real file (7,565 ticks).
- Spreads: OANDA median 1.5 pips vs Dukascopy 0.3 pips in that hour. Shadow results must use
  OANDA's own bid and ask.
- Dukascopy's terms of use for its free historical data have not been read yet.
- OANDA price stream (stream-fxpractice.oanda.com) needs its own key entry before the recorder can run in the cloud.
- Whether a separate OANDA token can be limited to one account, for the later execution service.
