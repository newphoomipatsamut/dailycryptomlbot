# Progress / Session Notes

Living log for continuity across Claude Code sessions. Newest entry on top.
Read this first before touching the bot or the backtest.

---

## 2026-07-11 (session 9) — Kill switch + real exit execution (stop-loss orders, TP/max-hold sells) — UNEXERCISED against a real order

User asked to start on stop-loss/kill-switch, one of the four live-trading
blockers named across sessions 6-8. Investigation before coding surfaced a
bigger gap than the request implied.

**Critical finding: there was no sell-execution code anywhere.**
`check_exits()` only computed what SHOULD happen on paper; `log_exit()`
only wrote to the Sheet. `place_live_order()` placed a real BUY but
nothing ever placed a real SELL. In live mode as it stood, the bot would
buy real crypto on a signal and then never actually sell it — the
position would sit open on the exchange indefinitely while the Sheet
said "CLOSED". Asked the user how to scope the fix; they chose to build
full exit execution, not just the stop-loss backstop alone (see
AskUserQuestion in the session transcript).

**API research (verified against primary sources before writing any
order code, not secondhand ccxt docstrings which said "margin only" and
turned out to be stale/inaccurate):**
- Read the installed ccxt `kraken.py` source directly — `stopLossPrice`
  param maps unconditionally to Kraken's native `stop-loss`/
  `stop-loss-limit` ordertypes via `order_request()`.
- Fetched Kraken's own Add Order API docs — `stop-loss`, `stop-loss-limit`
  etc. are documented spot ordertypes, no margin-only restriction stated.
- Also found Kraken's `close` param (attach a conditional order that
  auto-triggers on the primary order's fill, OCO-like) — considered it,
  but its own txid isn't in the create-order response (only discoverable
  after fill via a separate lookup) AND whether it works on spot vs
  margin-only is genuinely unconfirmed by either ccxt or Kraken's docs.
  Advisor input: the honest discriminator (place one tiny real limit buy
  with a `close` stop attached, see if Kraken accepts the shape) can't be
  run — no real Kraken credentials exist locally, only as GH Actions
  secrets. **Chose the separate stop-loss order path** (confirmed-legal)
  over the unconfirmed atomic `close` mechanism, with a mandatory
  fail-safe making up the difference (see below).

**What was built (`crypto_daily_ml_v3.py`):**

1. **Kill switch** — `KILL_SWITCH_DRAWDOWN=0.15` (~2-4x the backtest's
   historical max drawdown of -3.5% to -9%). Tracks `peak_balance` +
   `halted` in DailyMeta. Halts NEW entries only (doesn't force-liquidate
   — dumping at a bad tick could compound damage). Manual reset required
   (`halted` never auto-clears). **Fully testable in paper mode, fully
   verified**: 7 unit tests against an in-memory mock Sheets worksheet +
   3 full `run()` integration tests (mocked Sheets+exchange) — trips and
   blocks entries on a 16% drawdown, doesn't spuriously trip on a healthy
   balance, correctly resumes after manual reset. Committed separately as
   `fe5de6c` before starting the higher-risk exit-execution work.

2. **Resting stop-loss order at entry** (`place_stop_loss_order()`) — after
   a live buy fills, places `stop-loss` (market-on-trigger, NOT
   `stop-loss-limit` — a limit can gap through in a fast move and never
   fill, defeating the entire point of a crash backstop) sized to the
   ACTUAL filled qty (not the intended qty — taker fills can slip).
   **Mandatory fail-safe**: if stop placement fails for any reason, the
   position is flattened immediately via a market sell rather than held
   naked hoping tomorrow's run catches it — this is the real safety
   property the whole task exists for. Both outcomes (successful flatten,
   or flatten-also-fails) are logged as real Sheet rows with accurate
   PnL/balance — a real buy already happened, so neither path is a no-op.

3. **Real sell execution for TP/trailing/max-hold exits**
   (`place_live_sell()`) — `check_exits()` previously only computed the
   theoretical outcome; nothing executed it. Now market-sells the actual
   position and logs the ACTUAL fill price, not the theoretical TP/
   trail/maxhold price.

4. **Reconciliation, in the correct order** (`reconcile_stop_fills()`,
   `cancel_stop_before_exit()`) — a resting stop can fill between daily
   runs; the run has to learn about that from the exchange before
   `check_exits()` runs, or a position the exchange already sold gets
   double-counted as still open. Sequence: (a) for each open position
   with a real stop id, ask the exchange if it filled overnight — if so,
   log as a real SL exit at the actual fill price and remove from the
   list `check_exits()` sees; (b) run `check_exits()` on the genuinely-
   still-open remainder; (c) for any poll-driven exit (TP/trailing/max-
   hold), cancel the resting stop FIRST — and don't just assume the
   cancel succeeded. `cancel_stop_before_exit()` re-fetches order status
   on a cancel failure to distinguish "stop already filled" (that IS the
   real exit — do not sell again, log the stop's actual fill instead)
   from "fully unresolved" (do not sell blind — skip this run, retry
   next time, surface loudly). Handles 3 stop_order_id states correctly:
   real id, `''` (paper/pre-migration rows), `'FLATTEN_FAILED'` sentinel
   (an already-fully-logged entry-time failure, never queried as if it
   were a live order).

5. **Sheet schema**: `DailyTrades` widened 17->19 cols (`stop_order_id`,
   `fill_qty`), migration is idempotent and patches existing sheets'
   headers in place rather than requiring a fresh sheet.

**Verification — and its explicit limits.** The order-placement/
reconciliation code is gated on `not PAPER_MODE`, and the daily GH
Actions cron always runs `PAPER_MODE=true` — so the normal
commit->workflow_dispatch->read-log verification loop CANNOT exercise
any of this new code end-to-end against a real order. Per advisor
guidance: did not flip PAPER_MODE to test real fills (that's the
dangerous kind of scope creep for a change like this). Instead:
- Re-ran the full paper-mode `run()` integration test (mocked
  Sheets+exchange) after ALL the exit-execution changes — confirms the
  new code paths don't break the only path that actually runs in
  production. Passed clean, reconciliation correctly skipped entirely.
- Unit-tested `reconcile_stop_fills()` and `cancel_stop_before_exit()`
  against a mocked exchange covering: stop filled overnight, stop still
  resting, `fetch_order` transient failure (must NOT silently drop the
  position), cancel-races-fill (the stop beats the cancel — must NOT
  sell again), fully-unresolved cancel+fetch failure (must NOT sell
  blind), and both sentinel/empty stop_order_id states (must never
  query the exchange for these). All passed.

**Real-Sheet verification caught a real bug the mocks couldn't** —
`workflow_dispatch` run `29149338929` failed the header patch with
`Range (DailyTrades!R1) exceeds grid limits` (didn't crash the run, but
`stop_order_id`/`fill_qty` never got added). Root cause: the live sheet's
grid was still its original 16 columns; `get_or_create()`'s `cols=`
argument only sets width on a brand-new sheet, not an existing one. Fixed
by explicitly resizing before patching headers (`e277f76`), re-verified
clean on `workflow_dispatch` run `29149415483` (no error, header patch
silent-success, kill switch and exit/entry logic all correct). This is
exactly the category of bug mocks can't catch — worth remembering next
time a Sheet schema change looks "obviously fine" locally.

**LABEL THIS UNEXERCISED until validated against one small real order.**
Structurally correct + fail-safe + mock-tested is the ceiling reachable
without real capital or credentials. Three real bugs were caught and
fixed DURING this session by re-deriving the flow carefully (fail-safe
not logging the real round-trip trade, `log_exit`'s Sheet-write range
missing the new stop_order_id/fill_qty columns, `active_syms`/summary
counts not accounting for reconciled exits) — that pattern (repeated
"wait, real bug I just introduced" catches) is itself the signal that
blind inspection is near its useful limit for money-code with no way to
run it for real. **Before ever flipping PAPER_MODE=false: place one tiny
real order manually first and confirm the stop-loss mechanism actually
works as expected on Kraken** — mocks can't catch everything a real
exchange response might do differently.

**Explicitly not done this session:** did not validate the `close`
atomic OCO mechanism (deferred, unconfirmed on spot); did not touch
strategy/ML code; did not flip PAPER_MODE; did not reduce
`RISK_PER_TRADE` from 25% (still a live-trading blocker — sizing wasn't
in scope for this session, only stop-loss/kill-switch).

---

## 2026-07-11 (session 6) — Forward test (the go/no-go gate): NOT falsified, but NOT proven, and the model has gone dormant

User asked "can this trade live yet — I've run paper since February." Ran
the forward test (`forward_test.py`, new this session) comparing live
realized paper P&L against the backtest's prediction over the SAME window,
plus a buy-&-hold benchmark. Verdict: **not yet — three independent
blockers**, the most urgent of which is a new finding.

**The forward test, on the 10 real trades (exported DailyTrades CSV,
window 2026-03-12 → 2026-05-04, 53 days):**

| Metric            | LIVE (realized) | BACKTEST predicted (same window, no OFI) | Buy & hold ETH/SOL/LINK |
|-------------------|----------------:|-----------------------------------------:|------------------------:|
| Trades            | 10              | 15                                       | —                       |
| Return            | +3.8%           | +3.1%                                    | +4.4%                   |
| Win rate          | 60%             | 47%                                      | —                       |
| Stop-losses       | 0               | 4                                        | —                       |
| Max drawdown      | -0.2%           | -0.9%                                    | —                       |

**What it says (the honest read):**
1. **Bookkeeping is trustworthy.** 9/10 trades reconstruct identically via
   `check_exit()` on real Kraken prices. The 1 mismatch (SOL 2026-04-28:
   recorded `TRAIL_BE`, reconstructed `SL`) is exactly the documented
   partial-bar timing artifact from sessions 3/4, not a bug. The recorded
   live numbers are reliable.
2. **Not falsified.** Live return (+3.8%) is in the same ballpark as the
   backtest prediction (+3.1%) for the same window — the strategy roughly
   performed as simulated. Good sign, weak.
3. **But underperformed buy-&-hold** (+3.8% vs +4.4%) in the only window
   tested. Despite a favorable 0-stop-loss run, simply holding the three
   assets made more. (Partial excuse: the strategy is mostly in cash, so
   in up-markets it structurally lags — its only possible edge is downside
   protection, which this window didn't test.)
4. **n=10.** The 60% win / 0 SL is consistent with luck. 95% CI on a 60%
   win rate at n=10 spans ~26–88%; it's compatible with the backtest's
   true ~40%. Trade-level validation also failed (only 1 of 10 live trades
   overlapped with the FAST_MODE backtest's trade set — model-config
   mismatch, so only the aggregate comparison is meaningful).

**THE URGENT FINDING — the model has gone dormant:**
- `SIGNAL_THRESHOLD = 0.60` to fire (crypto_daily_ml_v3.py:120).
- The 10 trades that fired (Mar–Apr) had ensemble probs 0.611–0.757.
- **Every run in the last week output probs of 0.09–0.29** — less than
  half the firing threshold. Highest recent: 0.286.
- Consequence: **zero trades since 2026-05-04 — over 2 months of
  silence.** "Paper since February" is really "10 trades in a 7-week
  Mar–Apr window, then the model stopped firing." Recent scheduled runs
  all show 0 entries, 0/3 open.

A model whose probability outputs have collapsed to ~25% of threshold is
either (a) correctly sitting out an unfavorable regime, (b) suffering
concept drift (rolling-retrain features no longer match reality), or
(c) a data/feature break. Can't tell which without retraining locally
and inspecting feature distributions. Regardless: a dormant model can't
be validated and can't trade live.

**Verdict on "can it trade live": NO.** Three independent blockers:
1. Evidence is too thin (n=10, underperformed hold, see table).
2. **Model appears dormant — needs diagnosis first** (most urgent).
3. Mechanics still unsafe (no resting stop orders, no drawdown kill-switch,
   25% position sizing — unchanged from session 5's assessment).

**New tool committed: `forward_test.py`** — reusable. Loads an exported
DailyTrades CSV, reconstructs each trade's expected outcome (fidelity),
compares live vs backtest-predicted aggregates over the live window, and
benchmarks against equal-weight buy-&-hold. Re-run as live history grows:
`python forward_test.py --trades DailyTrades.csv --src kraken`.

**Explicitly not done:** did not diagnose the model dormancy (recommended
next step — retrain locally, inspect feature/prob distributions; does not
touch strategy). Did not run the FAST_MODE=False multi-regime backtest.
Did not add safety rails. Did not flip PAPER_MODE.

---

## 2026-07-11 (session 7) — Diagnosed model dormancy: real crash volatility destabilizing an over-fragile XGBoost, not drift or a bug

Followed up on session 6's urgent finding (model dormant, 0 trades since
2026-05-04). Traced ensemble/RF/XGB probability trajectories weekly from
late April through July via GH Actions logs (`gh run view --log`), then
reproduced `train_and_predict()` locally against live Kraken data to
inspect what's actually driving it.

**Initial read was wrong, caught by advisor review before writing it
down:** first pass concluded "gradual synchronized decay across all 3
symbols = high-variance noise, no real driver." That's internally
contradictory — pure per-symbol noise doesn't produce a *correlated*
multi-month decline across three independent models. Re-checked before
committing to either story.

**What's actually happening:**
1. **RF and XGB probabilities were traced separately** (not just the
   ensemble average). RF declined moderately (~0.50 in Apr/May → ~0.30 by
   July). **XGB collapsed severely** (~0.50 range → 0.02–0.18, sharply
   from ~2026-06-16 onward) — the ensemble average is being dragged down
   mostly by XGB, not a uniform effect.
2. **Real market cause found**: ETH/SOL/LINK all had a genuine flash-crash
   2026-06-02→06-05 (ETH -10.6% single-day, -19.3% over 2wk; SOL -8.7%/
   -23.0%; LINK -8.1%/-15.9% — confirmed via direct Kraken OHLCV, not
   model output). A **second**, even larger outlier already sits in the
   window: ETH -14.9% on 2026-02-05. `TRAIN_WINDOW=180` days is rolling,
   so both outliers are currently inside every live training window (Feb
   one rolls out ~Aug 2026, June one ~Dec 2026).
3. **XGBoost is structurally fragile to this, and always was** — checked
   its in-sample probability distribution at monthly cutoffs back to
   March: std≈0.39, range [0.02, 0.98], bimodal, at *every* checkpoint
   including when it was firing normally in Mar/Apr. 200 boosted trees,
   depth 4, on only 180 training rows / 28 features, with no
   regularization tuning — this was already overfit before the crashes;
   the outliers just pushed its already-unstable output toward the
   collapsed end and it's stayed there. `dow` (day-of-week — meaningless
   for a 24/7 market) and `fg_extreme_fear` showing up as top features
   every month is a consistent overfitting tell, not new.
4. **RF is comparably more robust** (bagging + `min_samples_leaf=5`) —
   declined too, but nowhere near as far, consistent with the same shared
   outlier cause hitting a less fragile model less hard.

**Verdict: the collapsed probabilities are the model reacting badly to
real extreme volatility sitting in a fragile 180-day window — not concept
drift in the "market permanently changed" sense, and not a code bug.**
Current outputs (0.09–0.29 vs a 0.60 threshold) can't be trusted as a
"no signal" read either way — they're a symptom of an outlier-sensitive
XGB config, not necessarily an honest read of current conditions.

**What this means for going live:** doesn't change the session-6 verdict
(still NO) — if anything it adds a 4th reason. But it reframes the fix:
this isn't "wait and see if it recovers" or "the edge never existed" —
it's a concrete, fixable overfitting/outlier-sensitivity problem
(winsorize extreme returns, add XGB regularization —
`reg_lambda`/`min_child_weight`/`gamma`, or shorten/robustify the
lookback). Whether to pursue that fix is a strategy decision — not made
this session, per standing "ask before tuning" rule.

**Explicitly not done:** no code changes. Did not implement any of the
candidate fixes above. Did not re-run the forward test (no new live
trades since session 6 — the model hasn't fired).

---

## 2026-07-11 (session 8) — Implemented the winsorize + XGB regularization fix (user-authorized)

User explicitly asked to implement the fix identified in session 7.
Applied to both `crypto_daily_ml_v3.py` and `backtest.py` (independently
duplicated `train_and_predict`, kept in sync per established convention):
- `winsorize_fit_apply()`: clips features to 1st/99th percentile, fit on
  the training split only (no lookahead into val/today).
- `XGBClassifier`: added `reg_lambda=5.0`, `min_child_weight=5`,
  `gamma=0.5`.

**A planned discriminator check (re-run the 10 known real trade entries,
check they still clear 0.60) turned out to be unusable and was dropped**:
0/10 failed to reproduce even at *unmodified baseline* params — the issue
is that faithful historical point-in-time reconstruction isn't achievable
locally (live's `fetch_ohlcv()` uses `limit=365` bars ending at the actual
run time; Fear&Greed is cached at today's range; Kraken's `since=` behaves
differently across calls). This is an environment/data-availability
limitation, not evidence the fix is wrong — confirmed by ruling out the
obvious causes (library version drift doesn't explain it, since *today's*
reproduction matched the live log exactly). Do not attempt to re-derive
historical live predictions locally again; it isn't reliable.

**What IS verified (same-environment, same-day comparisons — trustworthy):**
- XGB in-sample probability distribution: std 0.388→0.224, range
  [0.02,0.98]→[0.11,0.89]. No longer bimodal. Mechanically confirmed fix.
- Today's live probs (2026-07-11): XGB was 0.02–0.18 during the June/July
  collapse: 0.13 pre-fix →  **0.22–0.34 post-fix** (workflow_dispatch run
  `29148159484`, confirmed live, not just local). Un-collapsed.
- Kraken backtest, same window, pre-fix (session 7: 305 trades, 35.7%
  total) vs post-fix (187 trades, 15.9% total): trade count **−39%**,
  but win/SL/TP rates flat-to-slightly-worse (SL 21.0%→20.3%, TP
  37.7%→34.8%, TRAIL_BE 40.7%→44.9%). **Same shape as the session-2 OFI
  finding: cuts volume, does not improve quality.** The CAGR/Sharpe drop
  (16.7%→7.8%, 2.21→1.51) is NOT reported as a real effect — per the
  session-4 caveat, 25% position sizing amplifies small mechanical
  differences into large magnitude swings; only the rate-normalized
  numbers above are trustworthy.

**What this fix does NOT do:** it does not "un-dormant" the model or make
it trade again. Today's ensemble probs (0.26–0.40) are still below the
0.60 threshold — correctly, because conditions are genuinely uncertain
right now, not because of any remaining bug. It stabilizes the model's
output immediately instead of waiting for the crash outliers to roll out
of the 180-day window (~Aug/Dec 2026). The root cause — 200-tree XGB on
only 180 rows / 28 features — is mitigated, not fixed; `dow` (day-of-week,
economically meaningless for a 24/7 market) is still a top-3 feature
post-fix.

**Verified end-to-end:** `py_compile` both files clean. Committed
`fee9a46`, pushed. Live `workflow_dispatch` (run `29148159484`):
success, no errors, XGB probs 0.22–0.34 confirmed in the actual log
(not just local), 0 exits/0 entries (correct — still below threshold).

**Live-trading verdict: UNCHANGED (still NO)** — this fix addresses one
of the four blockers from session 6/7 (model output was untrustworthy);
the other three (thin evidence, unsafe mechanics — no resting stops/
kill-switch, 25% sizing) are untouched.

---

## 2026-07-11 (session 5) — Bumped GitHub Actions off Node 20 before the daily cron breaks

Two days after session 4, a check of the live runs surfaced one escalated
item: the **Node.js 20 deprecation warning went from cosmetic to active**.
GitHub EOL'd Node 20 in Apr 2026 and now forces these actions onto Node 24
by default on every run, emitting a `##[warning]` that names the offenders
by tag and points only at an `ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION`
escape hatch — i.e. hard removal is imminent, which would silently break
the `00:05 UTC` daily cron.

Live health otherwise: green. Runs `29138136084` (2026-07-11) and
`29068214219` (2026-07-10) both succeeded; the latest shows a normal flat
day (all three ML probs below threshold, 0 exits/entries, 0/3 open). The
session-4 exit-check fix continues to behave as designed in production
(runs land 3–4h into the UTC day, evaluating the most recently complete
bar — exactly the case it was written for).

**Fix (`.github/workflows/daily_ml.yml`, pushed as `c0ca1af`):**
- `actions/checkout@v3` → `@v5`
- `actions/setup-python@v4` → `@v6`

Picked the earliest stable Node-24 majors, not the bleeding edge (checkout
is now at v7, only ~3 weeks old — avoided on a live trading cron). Verified
each tag's declared runtime directly from its own `action.yml` `using:`
field rather than trusting secondhand advice:

| Action | v4 | v5 | v6 | v7 |
|---|---|---|---|---|
| `actions/checkout`   | node20 | **node24** | node24 | node24 |
| `actions/setup-python` | node16 | node20 | **node24** | — |

`setup-python@v6` is the *only* node24 major, so that one was forced;
`checkout@v5` was chosen as the smallest sufficient jump. Both are drop-ins
for this workflow (no special inputs, `python-version: '3.10'`).

**Verified end-to-end via `workflow_dispatch` (run `29142654357`):**
completed/success, step list now reads `Run actions/checkout@v5`, and the
full log has **zero** matches for `node 20` / `forced to run on Node` /
`deprecat` / `##[warning]` — warning fully silenced. Bot logic ran normally
(OFI gates evaluated, signals generated, sheet writes OK, no errors).

**Explicitly not done:** no code change to `crypto_daily_ml_v3.py` or
`backtest.py`; no strategy/threshold/model tuning (still on hold per
session 1); did not act on the OFI finding (still waiting on more live
data). The OFI live sample (n=20 as of session 3) has not meaningfully
grown — the last several runs have been flat (no entries), so there's
nothing new to re-run `analyze_live_ofi.py` against yet.

---

## 2026-07-09 (session 4) — Fixed the same bug in the LIVE bot, refreshed all backtest numbers

Session 3 found and fixed the entry-day trailing-stop bug in `backtest.py`
only. This session found and fixed the **same underlying issue in the live
bot itself** (`crypto_daily_ml_v3.py`), which is worse there, then
refreshed every stale backtest number from session 2/3 now that the fix is
in.

**Live bug (`crypto_daily_ml_v3.py::check_exits()`), pushed as `6d31c71`:**
GitHub Actions scheduled runs land ~4hrs into the UTC day on average
(measured across all 119 scheduled runs: range 2.9-6.3h, **never** close
to the scheduled 00:05 UTC — this is GitHub Actions' well-documented
unreliable scheduling, not a bot bug). So every single day, `check_exits()`
was checking TP/SL against a bar that's on average only ~18% complete.
Two real consequences:
1. **Delayed detection** — a real SL/TP hit that happens after the check
   isn't caught until the next day, which can flip the recorded outcome
   (this is the exact SOL 2026-04-28 case from session 3: a real SL
   breach got misrecorded as a `TRAIL_BE` save).
2. **Fully missed exits** (structural, can't be quantified from sheet data
   alone) — if price hits TP/SL after the check and reverses back inside
   the range before the next day's check, that hit is never detected at
   all; the position just keeps holding.

Fix: `check_exits()` now evaluates the most recently **complete** daily
bar (yesterday relative to the run) instead of today's partial one. Entry
price and signal generation are unchanged — this only changes exit
evaluation. Verified against all 8 known real trades: 7/8 unaffected
(identical outcome), and the 1 known-buggy case (SOL 2026-04-28) now
correctly resolves to `SL` instead of the erroneous `TRAIL_BE` — exactly
the failure mode being fixed. Confirmed clean on a live `workflow_dispatch`
run afterward, no errors.

**Refreshed backtest numbers** (session 2/3's tables were stale — this is
current as of the `backtest.py` entry-day fix, before this session's fix
existed in `backtest.py`'s own logic since that was already fixed in
session 3):

| Config | CAGR (pre both fixes) | CAGR (post backtest.py fix) | Sharpe | Trades | Win Rate |
|---|---:|---:|---:|---:|---:|
| Kraken ~2yr, FAST_MODE=True | 10.4% | 14.5% | 1.94 | 335 | 34.9% |
| Kraken ~2yr, FAST_MODE=False (exact) | 7.5% | **25.0%** | 2.61 | 460 | 37.6% |
| Binance 8yr, OFI proxy off | 24.6% | 43.5% | 4.31 | 1565 | 47.7% |
| Binance 8yr, OFI proxy on | 7.6% | 12.5% | 2.35 | 615 | 46.5% |

**Read this caveat before quoting any of these numbers elsewhere:** the
`backtest.py` fix didn't change the strategy — it corrected bookkeeping
(fewer trades wrongly marked as full stop-losses instead of breakeven
saves). At `RISK_PER_TRADE=0.25` compounded over hundreds-to-thousands of
trades across years, even a small per-trade bookkeeping correction
amplifies enormously (Binance-no-OFI Sharpe alone went 2.33→4.31). Treat
the *direction* (all four went up) as trustworthy; do not treat the exact
magnitudes as a real expected-return estimate. This is also a reminder
that 25% position sizing makes every backtest number extremely sensitive
to small mechanical details — worth remembering before ever scaling this
with real capital.

**Explicitly not done:** did not touch strategy/thresholds/features. Did
not re-run `analyze_live_ofi.py` (n is still 20, unchanged this session —
that finding is untouched by either fix since it derives its OFI-blocked
outcomes from the same corrected `check_exit()` logic backtest.py already
had in session 3).

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

- **Kill switch + real exit execution built (session 9), UNEXERCISED
  against a real order.** Resting stop-loss orders, real TP/max-hold
  sell execution, and overnight reconciliation are all implemented and
  mock-tested, but the order-placement code has NEVER run against a real
  Kraken order (gated on `PAPER_MODE=false`, which the daily cron never
  sets). Before ever flipping `PAPER_MODE=false`: place one tiny manual
  real order first and confirm the stop-loss mechanism behaves as
  expected — do not trust this code at full size on the strength of
  mocks alone. `RISK_PER_TRADE` is still 25% (unchanged, out of scope
  for session 9) — needs to come down before real capital regardless of
  how the exit-execution code performs.
- **Model dormancy DIAGNOSED (session 7) and MITIGATED (session 8)** —
  root cause: two real crash outliers (Feb, June 2026) destabilizing an
  already-overfit XGBoost. Winsorizing + XGB regularization implemented,
  committed `fee9a46`, verified live (probs un-collapsed 0.02-0.18 →
  0.22-0.34). Does NOT make the model trade — still correctly outputs
  <0.60 as of 2026-07-11, conditions are genuinely uncertain. Backtest
  shows the fix cuts trade volume (-39%) without improving win/SL/TP
  rates — same shape as the session-2 OFI finding. Root overfitting cause
  (28 features / 180 rows, no other regularization) is still present,
  just mitigated. If probabilities are still weirdly extreme/unstable
  after ~2026-08 (Feb outlier rolls out) or ~2026-12 (June one), that's a
  sign the mitigation isn't sufficient and needs revisiting.
- **Forward test verdict is in (session 6): NOT ready for live capital.**
  n=10 trades underperformed buy-&-hold (+3.8% vs +4.4%); bookkeeping is
  trustworthy (9/10 fidelity) but the evidence is too thin and the model is
  dormant. Re-run `forward_test.py` as live history grows — but history
  won't grow until the dormancy above is resolved.
- **Re-run `analyze_live_ofi.py` periodically as live history grows.**
  n=20 as of 2026-07-09 is not enough to trust the OFI-gate finding
  either direction (real data currently suggests the gate filters for
  quality — opposite of session 2's Binance-proxy finding — but treat
  that as a hint, not a conclusion, until n is much larger). Needs fresh
  `DailySignals`/`DailyTrades` CSV exports each time (see script docstring
  for why — no local API access to the sheet).
- **Four-config comparison table refreshed in session 4** (see above) —
  no longer stale as of 2026-07-09. `FAST_MODE=False` on the full Binance
  8yr history is still never run (all Binance numbers are FAST_MODE=True);
  would likely take multiple hours — worth doing before any real-money
  decision, not before.
- **Whether to act on either OFI finding** — e.g. reconsidering
  `OFI_GATE` threshold or gate design in the live bot — is a strategy
  decision, explicitly not made. Ask the user, and only once n is large
  enough to mean something.
- **Live's partial-bar exit-check timing bug is FIXED as of session 4**
  (`6d31c71`) — `check_exits()` now uses the most recently complete daily
  bar. If a live outcome ever looks surprising vs. what the backtest
  predicts going forward, this is no longer the likely cause; look
  elsewhere first.
- **Node.js 20 deprecation — RESOLVED in session 5** (`c0ca1af`):
  escalated from cosmetic to an active `##[warning]` (GitHub now forces
  Node 24). Bumped `checkout@v3`→`@v5`, `setup-python@v4`→`@v6` (verified
  via each action's `action.yml` `using:` field). Warning gone, run clean.
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
