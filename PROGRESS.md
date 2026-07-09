# Progress / Session Notes

Living log for continuity across Claude Code sessions. Newest entry on top.
Read this first before touching the bot or the backtest.

---

## 2026-07-09 (session 3) — Real live OFI answer, a genuine backtest bug fixed, tracking tool added

User asked to check the live `DailySignals`/`DailyTrades` Google Sheet (no
local API access to it — see below) to answer the OFI question with real
data instead of the Binance proxy from session 2. Found something more
useful than expected.

**Access note:** no local service-account credentials or sheet ID exist
(they're GitHub Actions secrets); Google Drive OAuth via `/mcp` and the
Chrome extension were both unavailable in this environment. User exported
`DailySignals` and `DailyTrades` to CSV manually (Google Sheets > File >
Download > CSV) and handed over the file paths — that's the path this
session used and the one `analyze_live_ofi.py` (see below) expects.

**Real finding (n=20, from actual live order-book OFI values, not a
proxy):** every day the ML model wanted to enter, split by whether the
live OFI gate passed (8, real trades) or blocked (12, reconstructed via
forward Kraken price data):

| Group | n | Mean pnl/trade | SL rate | TP rate |
|---|---:|---:|---:|---:|
| OFI passed (real trades) | 8 | +1.50% | 0% | 50% |
| OFI blocked (reconstructed) | 12 | +0.83% | 42% | 42% |

This is the **opposite** conclusion from session 2's Binance-proxy finding
("cuts volume, not quality") — here the real gate looks like it's
filtering for quality (zero raw stop-losses among what it passed).
**n=20 is nowhere near enough to trust this either way** — logged as a
hint to keep watching as more live history accumulates, not a conclusion.

**Real bug found and fixed in `backtest.py`, discovered while validating
the reconstruction against 8 known real outcomes (3 didn't match at
first):**
- `check_exit()`'s trailing-stop trigger only ever looks at days with
  `hold_days>=1` — it never evaluates the entry day's own high. Live's
  `check_exits()` (`crypto_daily_ml_v3.py`) scans `entry_date..yesterday`
  *inclusive of the entry day* for the breakeven trigger. So a trade that
  spiked 1.5%+ on its own entry day would get live's trailing-stop
  protection starting immediately, but backtest.py wouldn't apply it until
  day 2 — silently turning some real `TRAIL_BE` (breakeven) outcomes into
  false `SL` (real loss) outcomes in the backtest. **Fixed**: entry loop
  in `run_backtest()` now seeds `trailing_active` from the entry day's own
  high before the first `check_exit()` call, matching live exactly.
  Verified: 7/8 known real outcomes now match (was 5/8 before the fix).
- The 1 remaining mismatch (SOL, 2026-04-28) is a *different*, structural
  issue, not a bug to fix: live's cron runs ~4hr into the UTC day (not
  exactly the scheduled 00:05), so "today's" bar is still partial at
  check time — a same-day SL/TP breach that happens later that day isn't
  caught until the next day's run. This is inherent to checking a
  still-forming daily bar once per day; not something `backtest.py` can
  or should replicate (it uses complete historical bars, which is the
  more *correct* simulation for "what should have happened," just not an
  exact replica of live's real-time blind spot).
- **Impact on backtest numbers:** the fix increases realized `TRAIL_BE`
  saves and reduces raw `SL` hits across the board. A quick Kraken
  FAST_MODE=True check post-fix: **14.5% CAGR** (was 10.4% pre-fix,
  same config otherwise) — the session-2 four-config comparison table is
  now stale and wasn't fully rerun this session; regenerate if a fresh
  number set is needed (`DATA_SOURCE=binance|kraken
  OFI_GATE_ENABLED=true|false python3 backtest.py`, ~4 configs, several
  minutes to an hour total).

**New tool: `analyze_live_ofi.py`** — reusable script to answer the "does
the live OFI gate filter quality or volume" question as more real history
accumulates, without redoing this investigation from scratch:
```
python analyze_live_ofi.py --signals "DailySignals.csv" --trades "DailyTrades.csv"
```
Takes exported CSVs (see access note above), filters to days the ML model
wanted to enter, cross-references real trades for the OFI-passed group and
reconstructs (via the now-fixed `check_exit()`) for the OFI-blocked group,
prints a comparison table with an explicit sample-size warning below n=30.
Re-run this periodically — **the answer only gets more trustworthy as n
grows**, and right now n=20 is not enough to act on.

**Explicitly not done:** did not re-run the full session-2 four-config
comparison table post-fix (noted stale above). Did not change live
`crypto_daily_ml_v3.py` or make any strategy decision based on the n=20
finding — that's still a call for the user once more data exists.

---

## 2026-07-09 (session 2) — Deeper history via Binance, OFI gate backtested for real

**Correction to session 1 below:** the "Kraken only lists these pairs from
2024-07-19" claim was **wrong**. Re-tested properly: Kraken's public OHLC
REST endpoint hard-caps at **720 daily bars total, always ending at "now,"
regardless of the `since` param**. Confirmed by requesting `BTC/USD`
(traded on Kraken since 2013) with `since=8y-ago` and still getting only
the most recent 720 bars — same cutoff date as ETH/SOL/LINK. It's an API
retention cap, not a listing-date limit. `backtest.py`'s docstring is now
corrected in place (no more references to a Kraken listing date).

**What changed in `backtest.py`** (not yet pushed — see below):
- `DATA_SOURCE` env var (default `'binance'`): backtest now sources OHLCV
  from Binance by default, which has genuinely deep history for these
  pairs (ETH from 2018-07-12, LINK from 2019-01-16, SOL from 2020-08-11 —
  SOL's real listing date is the actual constraint here). This is
  **research-only** — live still trades on Kraken, unaffected. Set
  `DATA_SOURCE=kraken` to backtest strictly on the live venue's ~2yr
  window instead.
- `OFI_GATE_ENABLED` env var (default `false`) + `fetch_taker_buy_ratio()`:
  the OFI entry gate can now actually be backtested, via a proxy — daily
  aggressor trade-flow imbalance computed from Binance's
  `taker_buy_base_asset_volume` kline field, `(2*taker_buy_vol -
  total_vol)/total_vol`, same `[-1,+1]` range and sign convention as
  live's order-book OBI. **This is NOT the same metric as live's gate**
  (day-aggregate trade flow vs. an intraday order-book depth snapshot) —
  true historical L2 snapshots aren't available anywhere for free. Treat
  it as informative, not a live-equivalent.
- Output CSVs now suffixed by mode (`backtest_trades_binance.csv`,
  `backtest_trades_binance_ofi.csv`, etc.) so different configs don't
  clobber each other. `.gitignore` updated to wildcard both patterns.

**Results — four configurations run this session:**

| Config                                    | CAGR  | Sharpe | MaxDD | WinRate | PF   | Trades |
|--------------------------------------------|------:|-------:|------:|--------:|-----:|-------:|
| Kraken ~2yr, FAST_MODE=True (session 1)    | 10.4% | 1.38   | -4.1% | 45.4%   | 1.38 | 249    |
| Kraken ~2yr, FAST_MODE=False (exact)       |  7.5% | 0.76   | -8.4% | 40.5%   | 1.13 | 452    |
| Binance 8yr, OFI proxy OFF, FAST_MODE=True | 24.6% | 2.33   | -9.3% | 48.7%   | 1.41 | 1619   |
| Binance 8yr, OFI proxy ON,  FAST_MODE=True |  7.6% | 1.35   | -8.6% | 47.2%   | 1.42 | 616    |

**Key finding:** the OFI proxy gate cuts trade count by ~62% (1619→616)
but **win rate and profit factor barely move** (48.7%→47.2%, 1.41→1.42).
It's filtering *volume*, not improving *quality* — at least as measured by
this trade-flow proxy. That's a real, if imperfect, signal that the live
order-book OFI gate's main effect may be similar (fewer trades, not
necessarily better ones) — but it's a different metric, so treat this as
a hypothesis worth watching in live `DailySignals` data, not a proven
conclusion.

**Interpretation, not a single "true" number:** the two most trustworthy
figures (Kraken exact-mode 7.5% and Binance-with-OFI-proxy 7.6%) land
suspiciously close together despite very different samples (~2yr vs 8yr,
different exchange, different gate). That convergence is *some* evidence
that ~7-8% CAGR is a more defensible central estimate than the earlier
10.4% or 24.6% headline numbers, both of which came from either a short
favorable window or an OFI-disabled upper bound. Still FAST_MODE=True for
the Binance runs (n_est=50, retrain every 5d) — a `FAST_MODE=False` run on
the full 8yr Binance history was not attempted this session (would likely
take multiple hours).

**Process note — a bug in my own smoke test, not the shipped code:** an
early attempt to sanity-check the new code by monkeypatching
`bt.CANDLE_LIMIT = 200` after import produced a bogus "-0.1% CAGR, 26
trades" result. Cause: `fetch_ohlcv_full`'s `days` parameter defaults to
`CANDLE_LIMIT` bound *at function-definition time* (unaffected by a
post-import patch), so OHLCV still used the full 8yr window — but
`fetch_taker_buy_ratio` receives `CANDLE_LIMIT` as an explicit argument
read *at call time*, so it silently used the patched value (200 days).
Result: the OFI series only covered the most recent ~200 days; every
earlier date's lookup fell through to the `.get(..., 0.0)` default, which
fails a `>0` gate — blocking ~97% of the backtest for a reason that had
nothing to do with the actual OFI proxy. Caught by cross-checking the
blocked-trade count against a plain probability estimate before trusting
the number; re-ran clean via `DATA_SOURCE=binance OFI_GATE_ENABLED=true
python3 backtest.py` as a fresh subprocess to get the real 7.6%/616-trade
result above. Moral: don't trust in-process monkeypatch tests against a
module with mixed def-time/call-time config binding — run config changes
as a fresh subprocess via env vars instead.

**Explicitly not done:** no changes to `crypto_daily_ml_v3.py` (live bot
unaffected), no strategy/threshold/feature tuning, no decision made about
whether to act on the "OFI gate cuts volume not quality" finding — that's
a call for the user, not something to unilaterally implement.

---

## 2026-07-09 (session 1) — Correctness audit: fee mismatch, lookahead bias, gspread deprecation

**Note:** the "Kraken listing date" claim in this entry was later found to
be wrong — see the correction at the top of session 2 above.

**State at session start:** repo had an uncommitted fix in `backtest.py`
(lookahead-bias exclusion of today's row from training) plus two generated
CSVs (`backtest_equity.csv`, `backtest_trades.csv`) sitting untracked, left
over from a prior session that was never wrapped up.

**What was done (3 commits, all pushed to `origin/main`):**

1. `cd4d04e` — Fix lookahead bias in backtest, correct fee mismatch and stale claims
   - `backtest.py::train_and_predict`: training set now excludes the last
     (today) row. Its `target` was precomputed non-NaN over the full
     dataset — unlike live, where tomorrow hasn't happened yet — so
     including it leaked the label into training. Verified this was
     already the code state used to generate the checked-in CSVs (file
     mtime predates CSV mtime), so the ~10.4% CAGR number below already
     reflects the fix.
   - `crypto_daily_ml_v3.py`: live entries fill as **taker** (v4 already
     removed the post-only flag), but `log_exit()` was still charging
     `MAKER_FEE` (0.16%) instead of the taker rate (0.26%) — understated
     round-trip fees by ~20bps/trade. Renamed `MAKER_FEE` → `TAKER_FEE`
     and fixed the one call site. `backtest.py` already used `TAKER_FEE`
     correctly, so this brings live in line with the backtest.
   - Corrected stale/wrong docstring claims: feature count (was "31", is
     actually 28 — `FEATURE_COLS` comment already said 28, header didn't
     match), and removed the unsubstantiated "31-33% annual return"
     estimate that the backtest never actually produced.
   - Added `.gitignore` for `backtest_trades.csv` / `backtest_equity.csv`
     (regenerated outputs, shouldn't be tracked) and `__pycache__/`.

2. `0562bc9` — Fix gspread `update()` deprecation warning
   - gspread 6.x deprecated the old `update(range_name, values)`
     positional order in favor of `update(values, range_name)`.
   - Both call sites (`save_balance`, `log_exit`) now use named args
     (`range_name=`, `values=`), which works on old and new gspread and
     silences the warning that showed up in the GitHub Actions log.

**Verification performed:**
- `python3 -m py_compile` on both files after every change.
- Confirmed the ~2-year backtest data window is a **real Kraken listing
  limit**, not a pagination bug: `fetch_ohlcv(since=6y ago, limit=5)` on
  ETH/SOL/LINK-USDT all return an earliest bar of `2024-07-19`. Documented
  this in `backtest.py`'s docstring so nobody "fixes" it again later.
- Recomputed backtest metrics directly from the checked-in CSVs (before
  they were gitignored) to sanity-check the claim correction:
  **CAGR 10.4%, Sharpe 1.38, win rate 45.4%, profit factor 1.38,
  max drawdown -4.1%, 249 trades, over 2024-06 → 2026-06 (1.97yr)**.
  This is `FAST_MODE=True` (n_est=50, retrain every 5d) and
  **OFI-gate-disabled** (no historical order-book data available) — an
  upper-bound estimate, not what live would actually have produced.
- Triggered two manual `workflow_dispatch` runs on GitHub Actions after
  pushing (run IDs `28995398988`, `28995534453`) — both passed, no
  errors, balance/Sheets/signal logic all executed correctly, and the
  gspread warning is confirmed gone from the second run's log.

**Explicitly out of scope this session (user chose "harden, don't tune"):**
Feature engineering, threshold tuning, or model changes were **not**
attempted. Tuning anything on a ~2-year sample for a system that will
eventually touch real money is a real overfitting risk — that work should
only happen on explicit request, ideally with the OFI-gate-disabled
caveat resolved first (see Open Items).

---

## Open items / where to pick up next

- **Re-run `analyze_live_ofi.py` periodically as live history grows.**
  n=20 as of 2026-07-09 is not enough to trust the OFI-gate finding
  either direction (real data currently suggests the gate filters for
  quality — opposite of session 2's Binance-proxy finding — but treat
  that as a hint, not a conclusion, until n is much larger). Needs fresh
  `DailySignals`/`DailyTrades` CSV exports each time (see script docstring
  for why — no local API access to the sheet).
- **Session-2's four-config comparison table (Kraken/Binance x OFI on/off)
  is now stale** post the session-3 trailing-stop bug fix. Not fully
  rerun this session. Regenerate if a fresh number set matters:
  `DATA_SOURCE=binance|kraken OFI_GATE_ENABLED=true|false python3
  backtest.py`, 4 runs, several minutes to ~an hour total.
- **`FAST_MODE=False` on the full Binance 8yr history never run.** All
  Binance numbers so far are FAST_MODE=True (n_est=50, retrain every 5d).
  Would likely take multiple hours — worth doing before any real-money
  decision, not before.
- **Whether to act on either OFI finding** — e.g. reconsidering
  `OFI_GATE` threshold or gate design in the live bot — is a strategy
  decision, explicitly not made this session. Ask the user, and only once
  n is large enough to mean something.
- **Live's daily exit check runs against a partial "today" bar** (cron
  lands ~4hr into the UTC day, not exactly the scheduled 00:05) — a
  same-day SL/TP breach after that check isn't caught until the next
  day's run. Documented as a known live-only behavior in session 3, not
  fixed (nothing to fix — it's inherent to checking once/day against a
  still-forming bar; would need intraday checks to close, which is a much
  bigger change). Worth knowing about if a live outcome ever looks
  surprising vs. what the backtest would have predicted.
- **Node.js 20 deprecation warning in Actions logs** (from
  `actions/checkout@v3` / `actions/setup-python@v4`) — unrelated to bot
  code, cosmetic, not fixed. Bump to `@v4`/`@v5` if it starts actually
  failing.
- Feature/threshold/model tuning is still explicitly on hold — don't
  start without asking first, per session-1 user direction (unchanged).
- Bot is still `PAPER_MODE=true` in the workflow — no live capital at
  risk. Confirm this deliberately before ever flipping it.

---

## Quick orientation for a fresh session

- `crypto_daily_ml_v3.py` — the live/paper bot, run daily via
  `.github/workflows/daily_ml.yml` (cron `5 0 * * *` UTC +
  `workflow_dispatch`). Reads/writes state to a Google Sheet
  (`DailyTrades`, `DailySignals`, `DailyMeta` tabs) via `gspread`.
- `backtest.py` — standalone walk-forward simulator, same features/model/
  exit logic as live, run locally (`python backtest.py`), not part of CI.
  `DATA_SOURCE` env var picks the price source (`binance` default, deep
  history, research-only; `kraken` matches live's actual venue, ~2yr cap).
  `OFI_GATE_ENABLED` env var (default false) turns on a Binance
  trade-flow-imbalance proxy for the OFI gate — not the same metric as
  live's order-book snapshot, see module docstring.
- `analyze_live_ofi.py` — compares real live OFI-gate outcomes (passed
  vs. blocked) using exported `DailySignals`/`DailyTrades` CSVs. This is
  the ground-truth check for the OFI question, separate from and more
  trustworthy than `backtest.py`'s Binance proxy. See its docstring for
  usage and caveats (sample size, partial-bar timing).
- `requirements_daily.txt` — deps for all three scripts.
- No test suite exists. Verification so far has been: `py_compile`,
  manual `workflow_dispatch` runs, manually recomputing backtest metrics
  from output CSVs, and cross-checking new logic (OFI proxy, live-OFI
  reconstruction) against independent re-implementations / known real
  outcomes before trusting the numbers.
