# Progress / Session Notes

Living log for continuity across Claude Code sessions. Newest entry on top.
Read this first before touching the bot or the backtest.

---

## 2026-07-09 — Correctness audit: fee mismatch, lookahead bias, gspread deprecation

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

- **OFI gate is untested historically.** Backtest disables it entirely
  (upper bound). If a paid/alt order-book history source becomes
  available, backtesting the OFI gate would close the biggest gap between
  backtest and live performance claims.
- **`FAST_MODE=False` full run never done this session.** The 10.4% CAGR
  figure above is FAST_MODE (n_est=50, retrain every 5d). A slow, exact
  live-equivalent run (n_est=200, daily retrain, ~20-40min) would be a
  more trustworthy number before making any real-money decision.
- **Node.js 20 deprecation warning in Actions logs** (from
  `actions/checkout@v3` / `actions/setup-python@v4`) — unrelated to bot
  code, cosmetic, not fixed. Bump to `@v4`/`@v5` if it starts actually
  failing.
- **Strategy tuning is explicitly on hold** — see "out of scope" above.
  Don't start this without asking first.
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
  OFI gate always disabled here (no historical book data).
- `requirements_daily.txt` — deps for both scripts.
- No test suite exists. Verification so far has been: `py_compile`,
  manual `workflow_dispatch` runs, and manually recomputing backtest
  metrics from output CSVs.
