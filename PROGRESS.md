# Progress / Session Notes

Living log for continuity across Claude Code sessions. Newest entry on top.
Read this first before touching the bot or the backtest.

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

- **`FAST_MODE=False` on the full Binance 8yr history never run.** All
  session-2 Binance numbers are FAST_MODE=True (n_est=50, retrain every
  5d). Only the ~2yr Kraken window has an exact-mode number (7.5% CAGR).
  An exact-mode run on Binance's 8yr range would likely take multiple
  hours — worth doing before any real-money decision, not before.
- **OFI proxy vs. live's real gate — still an open question.** The proxy
  (Binance trade-flow imbalance) is *not* the same signal as live's
  order-book depth snapshot. The finding that the proxy cuts trade volume
  without improving win rate/profit factor is suggestive, not proven for
  live's actual gate. Live's `DailySignals` sheet now logs OFI value per
  day (has since v2) — once enough live history accumulates, that's the
  real ground truth to check this against, not any backtest proxy.
- **Whether to act on "OFI gate cuts volume, not quality"** — e.g.
  reconsidering `OFI_GATE` threshold or gate design in the live bot — is
  a strategy decision, explicitly not made this session. Ask the user.
- **`backtest.py`'s new Binance/OFI capability not yet committed** as of
  this note being written — check `git log` / `git status` on resume; if
  still uncommitted, that's unusual and should be investigated (probably
  means the session ended before wrap-up).
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
- `requirements_daily.txt` — deps for both scripts.
- No test suite exists. Verification so far has been: `py_compile`,
  manual `workflow_dispatch` runs, manually recomputing backtest metrics
  from output CSVs, and cross-checking new logic (OFI proxy) against an
  independent instrumented re-implementation before trusting its numbers.
