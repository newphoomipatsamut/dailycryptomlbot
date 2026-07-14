"""
KrakenQuant Daily ML Bot — v3
==============================
Daily timeframe crypto trading using Random Forest + XGBoost ensemble.

Why daily vs tick scalping:
  - Tick scalping: fee = 0.32% vs avg move = 0.02%  → fee is 1400% of edge
  - Daily signals: fee = 0.32% vs avg move = 2-4%   → fee is 8-16% of edge
  - OFI correctly predicted direction 92% on tick data
  - On daily bars, RSI + ATR + EMA + momentum = strong signal set

v6 (real-capital readiness, session 9-10):
  [RISK]  Added resting stop-loss orders, real TP/max-hold sell execution,
          and a drawdown kill switch — previously exits were pure paper
          bookkeeping with no real sell ever placed. UNEXERCISED against
          a real order — see PROGRESS.md session 9 before trusting at size.
  [CONFIG] RISK_PER_TRADE lowered 25% → 1% (see below) — 25% was a
          backtest-era choice never meant for live capital.

v5 fixes (from live-data audit):
  [BUG]   check_exits() checked TP/SL against "today's" daily bar, which is
          still partial when the bot runs (GitHub Actions scheduled runs
          land ~4hrs into the UTC day on average across 119 runs, never
          near the scheduled 00:05 UTC). Real intraday SL/TP hits after
          the check could go undetected until the next run or be missed
          entirely. Confirmed on a real trade (SOL, 2026-04-28): a real
          SL breach happened after that day's check, went undetected,
          and only got caught a day later looking like a TRAIL_BE save.
          Now checks the most recently COMPLETE bar (yesterday) instead.
          Entry price/signal generation unchanged — still uses live
          "today" price.

v3 additions (Fear & Greed + BTC Dominance):
  [FEATURE] Fear & Greed Index (alternative.me, free)
            fg_value, fg_extreme_fear, fg_extreme_greed, fg_momentum
            Extreme fear historically = strong mean-reversion buy signal
            Extreme greed = weak entry conditions
  [CONFIG]  RISK_PER_TRADE = 25% (confirmed by fee math on live runs)
  [CONFIG]  MAX_POSITIONS raised 2 → 3 (one per symbol)
  Fear & Greed fetched once per run and cached — no extra latency per symbol

v4 fixes (from audit):
  [BUG 1] Stale model: was trained on [-180:-20], predicted with 20-day-old params.
          Now retrains on full TRAIN_WINDOW before predicting today.
  [BUG 2] Trade size at exit used current balance, not entry balance — PnL drift.
          trade_size now stored in DailyTrades col Q and retrieved on close.
  [RISK]  Live order was post-only (maker). Removed oflags:post — now fills as
          taker on breakouts instead of being rejected at the worst moment.
  [MINOR] Removed dead btc_dom feature computation (excluded from FEATURE_COLS).
  [MINOR] Added candle-timing diagnostic log to catch partial-bar train/serve skew.

v2 fixes (from audit):
  [BUG 1] Balance persistence: stored in Sheets DailyMeta tab
  [BUG 2] OFI removed from FEATURE_COLS, used as entry gate instead
  [BUG 3] TP/SL checked on daily HIGH/LOW not close price
  [RISK]  Live order has 60s fill timeout + cancel
  [MINOR] Min bar validation, min order size, row_id exit matching

Strategy:
  - Runs once per day via GitHub Actions (cron: 5 0 * * *)
  - Fetches 365 days of OHLCV from Kraken
  - Fetches Fear & Greed history + BTC dominance (once per run)
  - Takes daily OFI snapshot — used as entry gate, not model feature
  - Engineers 28 features (24 technical + 4 sentiment)
  - RF + XGB ensemble: enter if P(up) > 0.60 AND OFI > 0
  - TP=3% (vs daily HIGH), SL=1% (vs daily LOW), max hold 5 days

Position sizing:
  - Trade size: 1% of balance per signal (RISK_PER_TRADE — lowered from
    25% in session 10 for real-capital readiness; 25% was validated
    against backtest metrics, not sized for real money)
  - Max 3 concurrent positions (one per symbol)
  - TP: 3%  SL: 1%  Max hold: 5 days

See backtest.py for measured performance (~10.4% CAGR over the ~2yr history
Kraken has for these USDT pairs, OFI gate disabled — an upper-bound estimate,
not a live-equivalent number). Treat any "expected return" figure as
unverified until backed by a FAST_MODE=False backtest run.

v7 (state store: Google Sheets -> git-committed CSV/JSON):
  Every prior session needing to analyze live results (OFI-gate check,
  forward_test.py) required a manual "export the Sheet to CSV and hand me
  the path" round-trip, since there was no local API/OAuth access to the
  Sheet. Replaced gspread with DailyTrades.csv / DailySignals.csv /
  DailyMeta.json written to the repo root and committed back by the GH
  Actions workflow each run (same pattern as tjr_trading's paper_trades.csv)
  -- `git pull` now gets fresh data with no export step, and no
  GOOGLE_CREDS_JSON secret to manage. Column names/shapes unchanged, so
  forward_test.py / analyze_live_ofi.py work unmodified against these files.

Setup:
  pip install ccxt pandas numpy scikit-learn xgboost

Environment variables:
  KRAKEN_API_KEY, KRAKEN_SECRET
  PAPER_MODE  ('true' default), PAPER_BALANCE (default 10000)
"""

import csv, os, json, logging, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import ccxt
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score
try:
    from xgboost import XGBClassifier
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/tmp/crypto_daily_ml.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
SYMBOLS          = ['ETH/USDT', 'SOL/USDT', 'LINK/USDT']

PAPER_MODE       = os.environ.get('PAPER_MODE', 'true').lower() == 'true'
STARTING_BALANCE = float(os.environ.get('PAPER_BALANCE', 10000.0))
RISK_PER_TRADE   = 0.01          # TOTAL portfolio risk budget, divided across
                                 # up to MAX_POSITIONS concurrently open
                                 # positions (see the trade_size line in run()
                                 # below) -- fixed a real bug where this was
                                 # applied per-position with no division, so
                                 # up to MAX_POSITIONS x this fraction of the
                                 # account could be at risk simultaneously.
                                 # Lowered from 0.25 for real-capital
                                 # readiness (session 10) — 25% was a
                                 # backtest-sizing choice never meant for live
                                 # capital: n=10 live trades is too thin to
                                 # trust, and the session-9 stop-loss/exit-
                                 # execution code is still unexercised
                                 # against a real order. Revisit upward only
                                 # once both have more runway, and only after
                                 # validating the mechanism with
                                 # validate_stop_loss.py first. See
                                 # PROGRESS.md sessions 10, 12.
TAKE_PROFIT_PCT  = 0.030
STOP_LOSS_PCT    = 0.010
MAX_HOLD_DAYS    = 5
MAX_POSITIONS    = 3             # one per symbol (raised from 2)
SIGNAL_THRESHOLD = 0.60          # ensemble P(up) threshold
OFI_GATE         = 0.0           # OFI snapshot must be > this to allow entry
                                 # (positive = buy pressure in order book)
BREAKEVEN_TRIGGER= 0.015         # once position up 1.5%, SL moves to breakeven
                                 # protects profits without widening initial SL
TAKER_FEE        = 0.0026        # v4 removed post-only (oflags:post), so entry
                                 # fills as taker, not maker — fee must match
MIN_ORDER_USDT   = 15.0          # Kraken minimum order value

KILL_SWITCH_DRAWDOWN = 0.15      # halt NEW entries if balance falls this far
                                 # below its all-time peak (2-4x the backtest's
                                 # historical max drawdown of -3.5% to -9%, so
                                 # it triggers on genuine breakdown, not normal
                                 # volatility). Existing positions still exit
                                 # normally — halting only blocks new risk.
                                 # Manual reset required (halted=false in
                                 # DailyMeta) — deliberately no auto-resume.

TRAIN_WINDOW     = 180
MIN_WARMUP_DAYS  = 80            # raised from 60 — ensures 60 train + 20 val
CANDLE_LIMIT     = 365

# ─────────────────────────────────────────────────────────────
# STORE — git-committed CSV/JSON (replaces Google Sheets, see v7 note)
# ─────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent

TRADES_CSV_HEADER = [
    'row_id', 'date', 'symbol', 'action', 'entry_price', 'exit_price',
    'pnl_gross', 'pnl_net', 'fees', 'reason',
    'hold_days', 'win', 'balance_after',
    'signal_prob', 'rf_prob', 'xgb_prob', 'trade_size', 'stop_order_id',
    'fill_qty',
]
SIGNALS_CSV_HEADER = [
    'date', 'symbol', 'close', 'ensemble_prob', 'rf_prob', 'xgb_prob',
    'signal_fired', 'reject_reason', 'ofi_value', 'ofi_gate_pass',
    'rsi', 'atr_pct', 'in_position',
]

def init_store():
    """
    Ensure DailyTrades.csv / DailySignals.csv / DailyMeta.json exist (with
    header, if new) in the repo root and return their paths. The GH Actions
    workflow commits these back after every run — same restart-safe,
    git-as-database pattern as tjr_trading's paper_trades.csv. Column names
    are unchanged from the old Sheets tabs, so forward_test.py /
    analyze_live_ofi.py (which parse exported-CSV shapes) work unmodified.
    """
    trades_path  = DATA_DIR / 'DailyTrades.csv'
    signals_path = DATA_DIR / 'DailySignals.csv'
    meta_path    = DATA_DIR / 'DailyMeta.json'

    if not trades_path.exists():
        with open(trades_path, 'w', newline='') as f:
            csv.writer(f).writerow(TRADES_CSV_HEADER)
    if not signals_path.exists():
        with open(signals_path, 'w', newline='') as f:
            csv.writer(f).writerow(SIGNALS_CSV_HEADER)
    if not meta_path.exists():
        meta_path.write_text('{}')

    logger.info(f'Store ready: {trades_path.name}, {signals_path.name}, {meta_path.name}')
    return trades_path, signals_path, meta_path


def _load_meta(meta_path) -> dict:
    try:
        return json.loads(Path(meta_path).read_text())
    except Exception as e:
        logger.error(f'Load meta failed: {e} — starting from empty state')
        return {}


def _save_meta(meta_path, meta: dict) -> None:
    try:
        Path(meta_path).write_text(json.dumps(meta, indent=2, sort_keys=True))
    except Exception as e:
        logger.error(f'Save meta failed: {e}')

# ─────────────────────────────────────────────────────────────
# FIX BUG 1: PERSISTENT BALANCE
# ─────────────────────────────────────────────────────────────
def load_balance(meta_path) -> float:
    """Load current balance from DailyMeta.json. Falls back to STARTING_BALANCE."""
    meta = _load_meta(meta_path)
    if 'balance' in meta:
        val = float(meta['balance'])
        logger.info(f'Loaded balance from store: ${val:.2f}')
        return val
    logger.info(f'Balance initialised: ${STARTING_BALANCE:.2f}')
    return STARTING_BALANCE

def save_balance(meta_path, balance: float) -> None:
    """Persist current balance to DailyMeta.json."""
    meta = _load_meta(meta_path)
    meta['balance'] = round(balance, 4)
    meta['balance_updated'] = datetime.now(timezone.utc).isoformat()
    _save_meta(meta_path, meta)

# ─────────────────────────────────────────────────────────────
# KILL SWITCH — halt new entries on a large drawdown from peak balance
# ─────────────────────────────────────────────────────────────
def load_kill_switch_state(meta_path) -> tuple:
    """
    Returns (peak_balance, halted). peak_balance defaults to
    STARTING_BALANCE if never set. halted defaults to False.
    """
    meta = _load_meta(meta_path)
    peak   = float(meta['peak_balance']) if str(meta.get('peak_balance', '')).strip() else STARTING_BALANCE
    halted = str(meta.get('halted', '')).strip().lower() == 'true'
    return peak, halted


def save_kill_switch_state(meta_path, peak_balance: float, halted: bool) -> None:
    """Persist peak_balance and halted to DailyMeta.json (same key shape as before)."""
    meta = _load_meta(meta_path)
    meta['peak_balance'] = round(peak_balance, 4)
    meta['halted'] = str(halted)
    meta['kill_switch_updated'] = datetime.now(timezone.utc).isoformat()
    _save_meta(meta_path, meta)


def check_kill_switch(balance: float, peak_balance: float) -> tuple:
    """
    Compares current balance against the all-time peak. Returns
    (new_peak_balance, should_halt, drawdown_pct). should_halt is True
    once drawdown crosses KILL_SWITCH_DRAWDOWN — caller is responsible
    for persisting halted=True and for NOT auto-clearing it (manual
    reset by design, see KILL_SWITCH_DRAWDOWN comment).
    """
    new_peak = max(balance, peak_balance)
    drawdown = (new_peak - balance) / new_peak if new_peak > 0 else 0.0
    should_halt = drawdown >= KILL_SWITCH_DRAWDOWN
    return new_peak, should_halt, drawdown

# ─────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────
def fetch_ohlcv(exchange, symbol: str, limit: int = CANDLE_LIMIT) -> pd.DataFrame:
    """Fetch daily OHLCV from Kraken. Returns empty DataFrame if insufficient data."""
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=limit)
        df  = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.date
        df  = df.drop('ts', axis=1).set_index('date').sort_index()
        df  = df[df['close'] > 0].dropna()

        # FIX MINOR: validate we have enough bars
        if len(df) < MIN_WARMUP_DAYS:
            logger.warning(f'  {symbol}: only {len(df)} bars, need {MIN_WARMUP_DAYS} — skip')
            return pd.DataFrame()

        run_date = datetime.now(timezone.utc).date()
        last_candle = df.index[-1]
        skew_note = ('partial bar' if last_candle >= run_date
                     else 'complete bar')
        logger.info(f'  {symbol}: {len(df)} daily candles ({df.index[0]} → {last_candle}) '
                    f'[last={skew_note}, run={run_date}]')
        return df
    except Exception as e:
        logger.error(f'OHLCV fetch failed {symbol}: {e}')
        return pd.DataFrame()

def fetch_ofi_snapshot(exchange, symbol: str) -> float:
    """
    Daily OFI snapshot from order book top 20 levels.
    Returns OBI in [-1, +1]. Positive = buy pressure.
    Used as an entry gate (not a model feature) — same logic as tick bot.
    """
    try:
        ob    = exchange.fetch_order_book(symbol, limit=20)
        bids  = ob['bids'][:20]
        asks  = ob['asks'][:20]
        # ccxt may return [price, size] or [price, size, count] depending on exchange
        tot_b = sum(row[1] for row in bids)
        tot_a = sum(row[1] for row in asks)
        obi   = (tot_b - tot_a) / (tot_b + tot_a) if (tot_b + tot_a) > 0 else 0.0
        logger.info(f'  {symbol} OFI={obi:+.4f} '
                    f'({"PASS" if obi > OFI_GATE else "BLOCK"} gate)')
        return float(obi)
    except Exception as e:
        logger.warning(f'OFI snapshot failed {symbol}: {e}')
        return 0.0

# ─────────────────────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────────────────────
def fetch_fear_greed(limit: int = 365) -> pd.Series:
    """
    Fetch Fear & Greed Index history from alternative.me (free, no API key).
    Returns a Series indexed by date with values 0-100.
      0-24  = Extreme Fear  (historically strong buy signal)
      25-49 = Fear
      50-74 = Greed
      75-100= Extreme Greed (historically weak/reversal signal)
    Falls back to neutral (50) on failure.
    """
    try:
        url  = f"https://api.alternative.me/fng/?limit={limit}&format=json"
        req  = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())['data']
        records = {
            pd.Timestamp(int(d['timestamp']), unit='s').date(): int(d['value'])
            for d in data
        }
        series = pd.Series(records, name='fg_value').sort_index()
        logger.info(f'Fear & Greed: {len(series)} days loaded '
                    f'({series.index[0]} → {series.index[-1]})')
        return series
    except Exception as e:
        logger.warning(f'Fear & Greed fetch failed: {e} — using neutral 50')
        return pd.Series(dtype=float, name='fg_value')


# Module-level cache — Fear & Greed fetched once per run, not once per symbol
_FG_CACHE: pd.Series = None

def get_fear_greed() -> pd.Series:
    global _FG_CACHE
    if _FG_CACHE is None:
        _FG_CACHE = fetch_fear_greed()
    return _FG_CACHE


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build 28 features from daily OHLCV + Fear & Greed sentiment.
    OFI is NOT a feature here — it's used as an entry gate in run().
    Target: next-day close > today's close (binary).
    """
    f = pd.DataFrame(index=df.index)

    # ── Price returns ──────────────────────────────────────────
    for n in [1, 2, 3, 5, 10, 20]:
        f[f'ret_{n}d'] = df['close'].pct_change(n)

    # ── Momentum ───────────────────────────────────────────────
    f['mom_5_20']  = f['ret_5d'] - f['ret_20d']
    f['mom_sign']  = np.sign(f['ret_5d'])

    # ── RSI 14 ────────────────────────────────────────────────
    delta = df['close'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    f['rsi']          = 100 - 100 / (1 + rs)
    f['rsi_oversold']  = (f['rsi'] < 30).astype(int)
    f['rsi_overbought']= (f['rsi'] > 70).astype(int)

    # ── ATR 14 (normalised) ───────────────────────────────────
    hl  = df['high'] - df['low']
    hpc = (df['high'] - df['close'].shift()).abs()
    lpc = (df['low']  - df['close'].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    f['atr_pct']        = atr / df['close']
    f['atr_pct_z']      = ((f['atr_pct'] - f['atr_pct'].rolling(60).mean()) /
                            f['atr_pct'].rolling(60).std().replace(0, np.nan))
    f['high_vol_regime']= (f['atr_pct'] >
                            f['atr_pct'].rolling(60).quantile(0.75)).astype(int)

    # ── EMA ratios ────────────────────────────────────────────
    ema20 = df['close'].ewm(span=20, adjust=False).mean()
    ema50 = df['close'].ewm(span=50, adjust=False).mean()
    f['price_ema20_ratio'] = df['close'] / ema20 - 1
    f['price_ema50_ratio'] = df['close'] / ema50 - 1
    f['ema20_ema50_ratio']  = ema20 / ema50 - 1

    # ── Bollinger bands ───────────────────────────────────────
    sma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    f['bb_position'] = (df['close'] - lower) / (upper - lower + 1e-10)
    f['bb_width']    = (upper - lower) / sma20

    # ── Volume ────────────────────────────────────────────────
    vol_ma = df['volume'].rolling(20).mean()
    vol_std= df['volume'].rolling(20).std()
    f['vol_z']     = (df['volume'] - vol_ma) / vol_std.replace(0, np.nan)
    f['vol_trend'] = df['volume'].pct_change(5)

    # ── Daily range ───────────────────────────────────────────
    f['hl_position'] = ((df['close'] - df['low']) /
                        (df['high'] - df['low'] + 1e-10))
    f['range_pct']   = (df['high'] - df['low']) / df['close']

    # ── Day of week ───────────────────────────────────────────
    f['dow'] = pd.to_datetime(f.index).dayofweek

    # ── Fear & Greed Index ────────────────────────────────────
    # Fetched once per run, aligned to OHLCV dates
    # Three features:
    #   fg_value:        raw 0-100 score normalised to [0,1]
    #   fg_extreme_fear: 1 when score ≤ 25 (strong mean-reversion buy signal)
    #   fg_extreme_greed:1 when score ≥ 75 (historically weak entry conditions)
    #   fg_momentum:     7-day change in sentiment
    fg = get_fear_greed()
    if len(fg) > 0:
        fg_aligned        = fg.reindex(pd.to_datetime(f.index).date)
        fg_aligned        = fg_aligned.ffill().fillna(50)   # FIX: ffill() not fillna(method=)
        fg_aligned.index  = f.index
        f['fg_value']        = fg_aligned.values / 100.0
        f['fg_extreme_fear'] = (fg_aligned.values <= 25).astype(int)
        f['fg_extreme_greed']= (fg_aligned.values >= 75).astype(int)
        fg_series            = pd.Series(fg_aligned.values, index=f.index)
        f['fg_momentum']     = fg_series.diff(7) / 100.0
    else:
        f['fg_value']         = 0.5
        f['fg_extreme_fear']  = 0
        f['fg_extreme_greed'] = 0
        f['fg_momentum']      = 0.0

    # ── Target: next day close > today's close ────────────────
    f['target'] = (df['close'].shift(-1) > df['close']).astype(int)

    # ── High/Low for intraday TP/SL check (not model features) ─
    # Kept in DataFrame for check_exits but not in FEATURE_COLS
    f['daily_high'] = df['high']
    f['daily_low']  = df['low']
    f['close']      = df['close']

    return f.dropna(subset=['ret_1d', 'rsi', 'atr_pct'])

# FIX BUG 2: OFI removed from FEATURE_COLS — it's a gate, not a feature
FEATURE_COLS = [
    'ret_1d', 'ret_2d', 'ret_3d', 'ret_5d', 'ret_10d', 'ret_20d',
    'mom_5_20', 'mom_sign',
    'rsi', 'rsi_oversold', 'rsi_overbought',
    'atr_pct', 'atr_pct_z', 'high_vol_regime',
    'price_ema20_ratio', 'price_ema50_ratio', 'ema20_ema50_ratio',
    'bb_position', 'bb_width',
    'vol_z', 'vol_trend',
    'hl_position', 'range_pct',
    'dow',
    # Sentiment (v3 additions — Fear & Greed only)
    # BTC rel-strength features removed: hurt ETH without helping SOL/LINK
    # ETH correlates too tightly with BTC for the relative signal to be useful
    'fg_value', 'fg_extreme_fear', 'fg_extreme_greed', 'fg_momentum',
]  # 28 features total

# ─────────────────────────────────────────────────────────────
# MODEL
# ─────────────────────────────────────────────────────────────
def winsorize_fit_apply(X_fit: np.ndarray, X_apply: np.ndarray,
                         lower: float = 0.01, upper: float = 0.99) -> tuple:
    """
    Clip both arrays to per-column [lower, upper] percentiles computed from
    X_fit only (no lookahead into X_apply). Caps single-day outlier rows
    (e.g. a -10% crash day) from dominating tree splits / feature scaling.
    """
    lo = np.percentile(X_fit, lower * 100, axis=0)
    hi = np.percentile(X_fit, upper * 100, axis=0)
    return np.clip(X_fit, lo, hi), np.clip(X_apply, lo, hi)


def train_and_predict(features_df: pd.DataFrame) -> dict:
    """
    Train RF + XGB on last TRAIN_WINDOW days, predict today.
    Walk-forward: train on [:−20], validate on [−20:], predict on today.
    """
    df = features_df.copy()

    # Training rows: must have known target (not today)
    train_df = df[df['target'].notna()].copy()

    if len(train_df) < MIN_WARMUP_DAYS:
        logger.info(f'  Not enough data ({len(train_df)} rows, need {MIN_WARMUP_DAYS})')
        return {'signal': False, 'reason': 'insufficient_data'}

    train_df = train_df.tail(TRAIN_WINDOW)

    X_all   = train_df[FEATURE_COLS].fillna(0).values
    y_all   = train_df['target'].values

    # Today — last row, no target
    X_today = df[FEATURE_COLS].fillna(0).iloc[-1:].values

    # Walk-forward split — 20-day holdout used for val_acc only
    split    = -20
    X_tr     = X_all[:split];   y_tr = y_all[:split]
    X_val    = X_all[split:];   y_val= y_all[split:]

    # Winsorize at 1st/99th percentile before scaling, fit on the training
    # split only (no lookahead into val/today). Caps the influence of
    # single-day crash outliers (e.g. the 2026-06-05 ETH -10.6% day) that
    # were destabilizing XGB — see PROGRESS.md session 7.
    X_tr, X_val = winsorize_fit_apply(X_tr, X_val)
    X_all, X_today = winsorize_fit_apply(X_all, X_today)

    # Validation scaler — fit on training split only
    val_scaler   = StandardScaler()
    X_tr_sc      = val_scaler.fit_transform(X_tr)
    X_val_sc     = val_scaler.transform(X_val)

    # Production scaler — refit on full window so the live prediction
    # sees the most recent 20 bars in both training and scaling context.
    prod_scaler  = StandardScaler()
    X_all_sc     = prod_scaler.fit_transform(X_all)
    X_tod_sc     = prod_scaler.transform(X_today)

    # ── Random Forest ─────────────────────────────────────────
    rf = RandomForestClassifier(
        n_estimators=200, max_depth=6, min_samples_leaf=5,
        class_weight='balanced', random_state=42, n_jobs=-1,
    )
    rf.fit(X_tr_sc, y_tr)
    rf_acc = accuracy_score(y_val, rf.predict(X_val_sc))
    rf.fit(X_all_sc, y_all)                          # retrain on full window
    rf_prob = float(rf.predict_proba(X_tod_sc)[0][1])

    # ── XGBoost / GradientBoosting fallback ───────────────────
    # reg_lambda/min_child_weight/gamma added to curb overfitting on a
    # 180-row/28-feature window — unregularized XGB was producing a
    # bimodal, unstable probability distribution (std~0.39) every month
    # checked back to March, not just during the recent crash. See
    # PROGRESS.md session 7.
    if HAS_XGB:
        model = XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            reg_lambda=5.0, min_child_weight=5, gamma=0.5,
            eval_metric='logloss', random_state=42, verbosity=0,
        )
    else:
        model = GradientBoostingClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, random_state=42,
        )
    model.fit(X_tr_sc, y_tr)
    xgb_acc = accuracy_score(y_val, model.predict(X_val_sc))
    model.fit(X_all_sc, y_all)                       # retrain on full window
    xgb_prob = float(model.predict_proba(X_tod_sc)[0][1])

    ensemble = (rf_prob + xgb_prob) / 2
    signal   = ensemble >= SIGNAL_THRESHOLD

    # Feature importance
    top5 = sorted(zip(FEATURE_COLS, rf.feature_importances_),
                  key=lambda x: x[1], reverse=True)[:5]

    logger.info(f'  RF  prob={rf_prob:.3f} val_acc={rf_acc:.2f}')
    logger.info(f'  XGB prob={xgb_prob:.3f} val_acc={xgb_acc:.2f}')
    logger.info(f'  Ensemble={ensemble:.3f} | Signal={"YES" if signal else "no"}')
    logger.info(f'  Top features: {[(f, round(v,3)) for f,v in top5]}')

    return {
        'signal':        signal,
        'ensemble_prob': ensemble,
        'rf_prob':       rf_prob,
        'xgb_prob':      xgb_prob,
        'rf_acc':        rf_acc,
        'xgb_acc':       xgb_acc,
        'train_rows':    len(train_df),
        'top_features':  top5,
    }

# ─────────────────────────────────────────────────────────────
# POSITION MANAGEMENT
# ─────────────────────────────────────────────────────────────
def _read_csv_rows(path) -> list:
    """dict-per-row read of a store CSV, mirroring gspread's get_all_records()."""
    try:
        with open(path, newline='') as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def load_open_positions(trades_path) -> list:
    """
    Load open positions from DailyTrades.csv.
    FIX MINOR: only scans last 90 days (not entire file history).
    """
    if trades_path is None:
        return []
    try:
        rows     = _read_csv_rows(trades_path)
        cutoff   = (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d')
        open_pos = [
            r for r in rows
            if r.get('action') == 'OPEN'
            and not str(r.get('exit_price', '')).strip()
            and str(r.get('date', '')) >= cutoff
        ]
        logger.info(f'Loaded {len(open_pos)} open position(s)')
        return open_pos
    except Exception as e:
        logger.error(f'Load positions failed: {e}')
        return []

def reconcile_stop_fills(exchange, open_positions: list, today: str) -> tuple:
    """
    Live-mode only. For every open position holding a real resting
    stop-loss order id, ask the exchange whether it has filled overnight
    (the exchange enforces the stop in real time; this daily run only
    learns about it after the fact). Must run BEFORE check_exits() —
    check_exits() only knows about store state and OHLCV, so a position
    the exchange already sold would otherwise get double-counted as
    still-open and re-evaluated for a phantom exit. See PROGRESS.md
    session 9.

    stop_order_id can be: a real Kraken order id, '' (paper-mode rows or
    pre-migration rows — never queried), or the 'FLATTEN_FAILED' sentinel
    (an entry-time failure already fully logged — never queried, it does
    not represent a live resting order).

    Returns (still_open, filled_exits):
      still_open   — positions whose stop has NOT filled; pass to
                     check_exits() as normal.
      filled_exits — dicts in the same shape check_exits() produces
                     (ready for log_exit()), for positions the exchange
                     already closed via the stop.
    """
    still_open   = []
    filled_exits = []

    for pos in open_positions:
        stop_id = str(pos.get('stop_order_id', '')).strip()
        if not stop_id or stop_id == 'FLATTEN_FAILED':
            still_open.append(pos)
            continue

        sym = pos['symbol']
        try:
            status = exchange.fetch_order(stop_id, sym)
        except Exception as e:
            # Can't confirm either way — err toward treating it as still
            # open (check_exits() will re-evaluate it against OHLCV; the
            # resting stop, if genuinely still there, remains protective
            # either way). Do NOT silently drop the position.
            logger.warning(f'  {sym}: fetch_order({stop_id}) failed ({e}) '
                           f'— treating as still open this run')
            still_open.append(pos)
            continue

        if status.get('status') == 'closed':
            entry_px   = float(pos['entry_price'])
            exit_px    = float(status.get('average') or 0.0) or entry_px * (1 - STOP_LOSS_PCT)
            pnl_pct    = (exit_px - entry_px) / entry_px
            entry_date = str(pos['date'])
            try:
                hold_days = (datetime.strptime(today, '%Y-%m-%d').date()
                            - datetime.strptime(entry_date, '%Y-%m-%d').date()).days
            except Exception:
                hold_days = 0
            logger.info(f'  RECONCILE {sym}: resting stop {stop_id} filled '
                       f'overnight @ {exit_px:.4f} (pnl={pnl_pct*100:+.2f}%)')
            filled_exits.append({
                **pos,
                'exit_price': exit_px,
                'exit_date':  today,
                'pnl_pct':    pnl_pct,
                'hold_days':  hold_days,
                'reason':     'SL',   # a filled resting stop IS a real stop-loss
                'trailing_active': False,
            })
        else:
            # Still resting — genuinely open, check_exits() may still
            # trigger TP/trailing/max-hold against it.
            still_open.append(pos)

    return still_open, filled_exits


def check_exits(open_positions: list, ohlcv_data: dict, today: str) -> list:
    """
    Check TP/SL/max-hold exits for all open positions.
    Uses YESTERDAY's (most recently COMPLETE) daily HIGH/LOW for TP/SL —
    not "today's" bar. GitHub Actions scheduled runs land ~4hrs into the
    UTC day on average (confirmed empirically across 119 runs: 2.9-6.3h,
    never near the scheduled 00:05 UTC), so "today's" bar is still
    partial at check time — real intraday TP/SL hits after the check
    could go undetected until the next run (delayed by a day, changing
    e.g. a real SL into an apparent breakeven save), or be missed
    entirely if price reversed back inside the range before then. Entry
    price and signal generation still use "today's" live price — this
    only changes how EXITS are evaluated, not entries.

    Trailing stop logic:
      Once daily HIGH reaches entry × (1 + BREAKEVEN_TRIGGER),
      the effective SL is raised to entry price (breakeven).
      Converts "almost won, gave it back" trades into zero-cost exits.
      Checked in order: TP first, then trailing SL, then fixed SL.
    """
    to_close = []
    for pos in open_positions:
        sym        = pos['symbol']
        entry_px   = float(pos['entry_price'])
        entry_date = str(pos['date'])

        if sym not in ohlcv_data or ohlcv_data[sym].empty:
            continue

        df       = ohlcv_data[sym]
        today_dt = datetime.strptime(today, '%Y-%m-%d').date()
        check_dt = today_dt - timedelta(days=1)   # most recently complete bar

        try:
            entry_dt  = datetime.strptime(entry_date, '%Y-%m-%d').date()
            hold_days = (check_dt - entry_dt).days
        except Exception:
            hold_days = 0

        if hold_days < 1:
            # Entered today or yesterday — no complete bar to check yet.
            logger.info(f'  HOLD {sym} | entered too recently for a complete bar')
            continue

        # Most recently complete candle (yesterday relative to this run).
        # Deliberately no fallback to the latest row here — that would be
        # today's partial bar again, reintroducing the bug this fixes.
        check_row = df[df.index == check_dt]
        if check_row.empty:
            logger.warning(f'  {sym}: no complete candle for {check_dt} — skip check')
            continue

        daily_high = float(check_row['high'].iloc[0])
        daily_low  = float(check_row['low'].iloc[0])
        close_px   = float(check_row['close'].iloc[0])

        tp_price = entry_px * (1 + TAKE_PROFIT_PCT)
        sl_price = entry_px * (1 - STOP_LOSS_PCT)

        # ── Trailing stop: check if price ever touched breakeven trigger ──
        # Scan all COMPLETE candles from entry date up to (not including)
        # the day being checked, to see if HIGH ever reached
        # entry × (1 + BREAKEVEN_TRIGGER) on any earlier day, including
        # the entry day itself. If yes, effective SL is raised to entry
        # price (breakeven).
        be_trigger_px = entry_px * (1 + BREAKEVEN_TRIGGER)
        trailing_active = False
        try:
            entry_dt_ts = pd.Timestamp(entry_date).date()
            hist = df[(df.index >= entry_dt_ts) & (df.index <= check_dt)]
            if len(hist) > 1:  # at least one prior candle besides the checked day
                prior = hist.iloc[:-1]  # exclude the day being checked
                if (prior['high'] >= be_trigger_px).any():
                    trailing_active = True
        except Exception:
            pass

        effective_sl = entry_px if trailing_active else sl_price

        # ── Exit logic ────────────────────────────────────────────────────
        tp_hit  = daily_high >= tp_price
        sl_hit  = daily_low  <= effective_sl

        reason     = None
        exit_price = close_px
        exit_pnl   = (close_px - entry_px) / entry_px

        if tp_hit:
            reason     = 'TP'
            exit_price = tp_price
            exit_pnl   = TAKE_PROFIT_PCT
        elif sl_hit:
            if trailing_active:
                reason     = 'TRAIL_BE'          # trailing stop at breakeven
                exit_price = entry_px             # exits at entry = $0 gross
                exit_pnl   = 0.0
            else:
                reason     = 'SL'
                exit_price = sl_price
                exit_pnl   = -STOP_LOSS_PCT
        elif hold_days >= MAX_HOLD_DAYS:
            reason   = f'MAX_HOLD_{hold_days}d'
            exit_pnl = (close_px - entry_px) / entry_px

        if reason:
            to_close.append({
                **pos,
                'exit_price':       exit_price,
                'exit_date':        str(check_dt),
                'pnl_pct':          exit_pnl,
                'hold_days':        hold_days,
                'reason':           reason,
                'trailing_active':  trailing_active,
            })
            trail_str = ' [TRAIL]' if trailing_active else ''
            logger.info(f'  EXIT {sym} [{reason}]{trail_str} | '
                        f'entry={entry_px:.4f} exit={exit_price:.4f} '
                        f'pnl={exit_pnl*100:+.2f}% hold={hold_days}d')
        else:
            current_pnl = (close_px - entry_px) / entry_px
            trail_str   = ' TRAIL✓' if trailing_active else ''
            logger.info(f'  HOLD {sym}{trail_str} | '
                        f'pnl={current_pnl*100:+.2f}% hold={hold_days}d | '
                        f'TP@{tp_price:.4f} '
                        f'SL@{effective_sl:.4f}{"(BE)" if trailing_active else ""}')

    return to_close

# ─────────────────────────────────────────────────────────────
# TRADE LOGGING
# ─────────────────────────────────────────────────────────────
def _next_row_id(trades_path) -> str:
    """Generate a unique row ID for each trade entry."""
    try:
        with open(trades_path, newline='') as f:
            n = sum(1 for _ in f)  # includes header, matching old get_all_values() count
        return f'T{n:04d}'
    except Exception:
        return f'T{int(datetime.now().timestamp())}'

def log_signal(signals_path, today: str, symbol: str,
               close_px: float, signal: dict,
               ofi_val: float, features_row: pd.Series,
               signal_fired: bool, reject_reason: str,
               in_position: bool) -> None:
    """
    Log every symbol's daily signal to DailySignals.csv — even when no trade
    fires. This is the rejection log: shows WHY each symbol didn't trade each
    day. reject_reason values:
      NONE              — signal fired, trade entered
      PROB_TOO_LOW      — ensemble prob below SIGNAL_THRESHOLD
      OFI_NEGATIVE      — ML signal but OFI gate blocked entry
      ALREADY_IN_POS    — already holding this symbol
      MAX_POSITIONS     — at max concurrent positions
      INSUFFICIENT_DATA — not enough bars to train
    """
    if signals_path is None:
        return
    try:
        with open(signals_path, 'a', newline='') as f:
            csv.writer(f).writerow([
                today,
                symbol,
                round(close_px, 6),
                round(signal.get('ensemble_prob', 0), 4),
                round(signal.get('rf_prob', 0), 4),
                round(signal.get('xgb_prob', 0), 4),
                signal_fired,
                reject_reason,
                round(ofi_val, 4),
                'YES' if ofi_val > OFI_GATE else 'NO',
                round(float(features_row.get('rsi', 0)), 2),
                round(float(features_row.get('atr_pct', 0)) * 100, 4),
                in_position,
            ])
    except Exception as e:
        logger.error(f'Log signal failed: {e}')


def log_entry(trades_path, signals_path, today: str, symbol: str,
              entry_price: float, signal: dict,
              ofi_val: float, balance: float,
              features_row: pd.Series, trade_size: float,
              stop_order_id: str = '', fill_qty: float | str = '') -> str:
    """Log new trade entry to DailyTrades.csv. Returns row_id for later exit update."""
    row_id = _next_row_id(trades_path) if trades_path else 'T0000'

    if trades_path:
        try:
            with open(trades_path, 'a', newline='') as f:
                csv.writer(f).writerow([
                    row_id, today, symbol, 'OPEN',
                    round(entry_price, 6), '',
                    '', '', '', '',
                    '', '', round(balance, 2),
                    round(signal['ensemble_prob'], 4),
                    round(signal['rf_prob'], 4),
                    round(signal['xgb_prob'], 4),
                    round(trade_size, 4),             # col Q — used by log_exit
                    stop_order_id,                    # col R — resting stop order id, live-mode only
                    round(fill_qty, 8) if fill_qty != '' else '',  # col S — actual filled base-asset qty, live-mode only
                ])
        except Exception as e:
            logger.error(f'Log entry failed: {e}')

    # Signal log: fired = True, no reject reason
    log_signal(signals_path, today, symbol, entry_price, signal,
               ofi_val, features_row,
               signal_fired=True, reject_reason='NONE',
               in_position=False)

    return row_id

def log_exit(trades_path, pos: dict, balance: float) -> float:
    """
    Log trade exit. Returns updated balance.
    Uses trade_size stored at entry (col Q) so PnL is calculated on the
    actual allocated amount, not the current balance.
    """
    stored = pos.get('trade_size', '')
    trade_size = float(stored) if stored else balance * RISK_PER_TRADE / MAX_POSITIONS
    pnl_gross  = pos['pnl_pct'] * trade_size
    fees       = trade_size * TAKER_FEE * 2
    pnl_net    = pnl_gross - fees
    win        = pnl_net > 0
    new_bal    = balance + pnl_net

    logger.info(f'  CLOSED {pos["symbol"]} [{pos["reason"]}] | '
                f'gross=${pnl_gross:+.2f} fee=${fees:.2f} '
                f'net=${pnl_net:+.2f} bal=${new_bal:.2f}')

    if trades_path:
        try:
            with open(trades_path, newline='') as f:
                rows = list(csv.reader(f))
            row_id = str(pos.get('row_id', ''))
            for i, row in enumerate(rows):
                if i == 0:
                    continue  # header
                # Match by row_id (col A) if available, else fall back to
                # date+symbol+OPEN+empty exit (original method)
                match = (row_id and row and row[0] == row_id) or (
                    not row_id and len(row) > 4
                    and row[1] == pos['date']
                    and row[2] == pos['symbol']
                    and row[3] == 'OPEN'
                    and not row[5]
                )
                if match:
                    rows[i] = [
                        row[0],                               # row_id preserved
                        pos['date'], pos['symbol'], 'CLOSED',
                        round(float(pos['entry_price']), 6),
                        round(pos['exit_price'], 6),
                        round(pnl_gross, 4), round(pnl_net, 4),
                        round(fees, 4), pos['reason'],
                        pos['hold_days'], str(win), round(new_bal, 2),
                        row[13] if len(row) > 13 else '',
                        row[14] if len(row) > 14 else '',
                        row[15] if len(row) > 15 else '',
                        row[16] if len(row) > 16 else round(trade_size, 4),
                        # col R: stop_order_id cleared on close — the resting
                        # stop must already be cancelled (or itself the
                        # trigger for this exit) by the time log_exit runs.
                        # See check_exits()/run() reconciliation.
                        '',
                        # col S: fill_qty cleared — position is fully closed,
                        # nothing left to reference the base-asset qty for.
                        '',
                    ]
                    break
            with open(trades_path, 'w', newline='') as f:
                csv.writer(f).writerows(rows)
        except Exception as e:
            logger.error(f'Log exit failed: {e}')

    return new_bal

# ─────────────────────────────────────────────────────────────
# LIVE ORDER (with fill confirmation)
# ─────────────────────────────────────────────────────────────
def place_live_order(exchange, symbol: str, trade_size: float,
                     entry_px: float) -> dict | None:
    """
    Place limit buy with 60s fill timeout. No post-only flag so the order
    fills immediately as a taker if price has moved — avoids being rejected
    on the breakouts where the ML signal fires.
    Returns {'fill_price': float, 'fill_qty': float} if filled, else None.
    fill_qty comes from the exchange's own 'filled' field — NOT recomputed
    from trade_size/entry_px — so the caller sizes any dependent order
    (e.g. the stop-loss) off what was actually bought, not what was intended.
    """
    import time
    try:
        qty   = trade_size / entry_px
        order = exchange.create_limit_buy_order(
            symbol, qty, entry_px   # no oflags:post — taker fill allowed
        )
        order_id = order['id']
        logger.info(f'  Order placed: {order_id} | {qty:.6f} {symbol} @ {entry_px}')

        # Poll for fill — up to 60 seconds
        for _ in range(12):   # 12 x 5s = 60s
            time.sleep(5)
            status = exchange.fetch_order(order_id, symbol)
            if status['status'] == 'closed':
                fill_px  = float(status.get('average') or entry_px)
                fill_qty = float(status.get('filled') or qty)
                logger.info(f'  FILLED {fill_qty:.6f} {symbol} @ {fill_px:.4f}')
                return {'fill_price': fill_px, 'fill_qty': fill_qty}
            if status['status'] == 'canceled':
                logger.warning(f'  Order {order_id} was cancelled')
                return None

        # Timeout — cancel
        exchange.cancel_order(order_id, symbol)
        logger.warning(f'  Order {order_id} timed out — cancelled')
        return None

    except Exception as e:
        logger.error(f'  Live order failed: {e}')
        return None


def place_stop_loss_order(exchange, symbol: str, qty: float,
                          stop_px: float) -> str | None:
    """
    Place a resting stop-loss-market order on Kraken for an already-filled
    long position — the crash backstop for the once-daily check_exits()
    poll (see PROGRESS.md session 9: a resting exchange-side stop protects
    against intraday moves the daily poll can't see until the next run).

    Uses 'stop-loss' (market-on-trigger), not 'stop-loss-limit': a limit
    order can gap through in a fast move and never fill, which would
    silently defeat the whole point of this backstop. A guaranteed exit
    with slippage beats a protected-on-paper position that never actually
    sells.

    stopLossPrice is a documented Kraken spot ordertype (stop-loss /
    stop-loss-limit are listed for the Add Order endpoint, no margin-only
    restriction in Kraken's own docs — see PROGRESS.md session 9 for the
    verification trail). NOT exercised against a real order yet — treat
    as unverified until confirmed with one small live trade.

    Returns the stop order's id, or None on failure. Caller MUST treat
    None as "position is unprotected" and fail safe (flatten immediately
    via place_live_sell) — never hold a live position with no resting
    stop and just hope the next day's run catches it.
    """
    try:
        order = exchange.create_order(
            symbol, 'market', 'sell', qty, None,
            {'stopLossPrice': stop_px}
        )
        order_id = order['id']
        logger.info(f'  Stop-loss placed: {order_id} | {qty:.6f} {symbol} '
                    f'trigger@{stop_px:.4f}')
        return order_id
    except Exception as e:
        logger.error(f'  Stop-loss placement failed: {e}')
        return None


def cancel_stop_before_exit(exchange, order_id: str, symbol: str) -> dict:
    """
    Cancel a resting stop-loss order before executing a poll-driven exit
    (TP/trailing/max-hold) — must run first, or the stop can fill
    concurrently and the position gets sold twice. A cancel failure is
    NOT automatically safe to ignore: it can mean the stop already
    filled (which IS the real exit — must not sell again), so this
    re-fetches order status on failure to tell the two cases apart
    instead of assuming success.

    Returns {'cancelled': bool, 'already_filled': bool, 'fill_price': float|None}.
    Caller logic:
      cancelled=True                    -> proceed with the poll-driven sell.
      already_filled=True               -> do NOT sell; log the SL exit at
                                            fill_price instead (the stop was
                                            the real exit, not the poll logic).
      both False (fetch_order also failed) -> unresolved; do NOT sell blind,
                                            surface loudly for manual check.
    """
    if not order_id or order_id == 'FLATTEN_FAILED':
        return {'cancelled': True, 'already_filled': False, 'fill_price': None}
    try:
        exchange.cancel_order(order_id, symbol)
        logger.info(f'  Cancelled resting stop {order_id}')
        return {'cancelled': True, 'already_filled': False, 'fill_price': None}
    except Exception as e:
        logger.warning(f'  Cancel {order_id} failed ({e}) — checking if it '
                       f'already filled...')
        try:
            status = exchange.fetch_order(order_id, symbol)
            if status.get('status') == 'closed':
                fill_px = float(status.get('average') or 0.0) or None
                logger.warning(f'  {symbol}: stop {order_id} had ALREADY '
                               f'FILLED @ {fill_px} — that is the real exit, '
                               f'not the poll-driven one about to be skipped')
                return {'cancelled': False, 'already_filled': True, 'fill_price': fill_px}
        except Exception as e2:
            logger.error(f'  {symbol}: could not confirm stop {order_id} '
                        f'status after cancel failure ({e2}) — unresolved, '
                        f'manual check required')
        return {'cancelled': False, 'already_filled': False, 'fill_price': None}


def place_live_sell(exchange, symbol: str, qty: float) -> dict | None:
    """
    Market-sell an existing long position to close it — used for TP/
    trailing/max-hold exits (check_exits() only computes what SHOULD
    happen; this is what actually executes it against the exchange), and
    as the fail-safe flatten when a resting stop-loss can't be confirmed.
    Market, not limit: on an exit we want certainty of fill over price,
    same reasoning as place_stop_loss_order.
    Returns {'fill_price': float, 'fill_qty': float} if filled, else None.
    """
    import time
    try:
        order = exchange.create_market_sell_order(symbol, qty)
        order_id = order['id']
        logger.info(f'  Market sell placed: {order_id} | {qty:.6f} {symbol}')

        # Market orders normally fill immediately, but poll briefly to
        # confirm and get the actual fill price for accurate PnL logging.
        for _ in range(6):   # 6 x 2s = 12s
            time.sleep(2)
            status = exchange.fetch_order(order_id, symbol)
            if status['status'] == 'closed':
                fill_px  = float(status.get('average') or 0.0)
                fill_qty = float(status.get('filled') or qty)
                logger.info(f'  SOLD {fill_qty:.6f} {symbol} @ {fill_px:.4f}')
                return {'fill_price': fill_px, 'fill_qty': fill_qty}

        logger.error(f'  Market sell {order_id} did not confirm closed '
                     f'within 12s — check exchange manually')
        return None

    except Exception as e:
        logger.error(f'  Live sell failed: {e}')
        return None

# ─────────────────────────────────────────────────────────────
# MAIN DAILY RUN
# ─────────────────────────────────────────────────────────────
def run():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    logger.info('=' * 60)
    logger.info(f'  KrakenQuant Daily ML v4 — {today}')
    logger.info(f'  Mode    : {"PAPER" if PAPER_MODE else "LIVE"}')
    logger.info(f'  XGBoost : {"yes" if HAS_XGB else "GradientBoosting fallback"}')
    logger.info(f'  Symbols : {SYMBOLS}')
    logger.info('=' * 60)

    # ── Connect ───────────────────────────────────────────────
    trades_path, signals_path, meta_path = init_store()

    # FIX BUG 1: load balance from persistent store
    balance = load_balance(meta_path)
    logger.info(f'Balance loaded: ${balance:.2f}')

    peak_balance, halted = load_kill_switch_state(meta_path)
    if halted:
        logger.warning(f'  KILL SWITCH ACTIVE — new entries blocked '
                        f'(balance=${balance:.2f} peak=${peak_balance:.2f}). '
                        f'Manual reset required (set halted=false in DailyMeta).')

    exchange = ccxt.kraken({
        'apiKey':          os.environ.get('KRAKEN_API_KEY', ''),
        'secret':          os.environ.get('KRAKEN_SECRET', ''),
        'enableRateLimit': True,
    })

    # ── Load open positions ────────────────────────────────────
    open_positions = load_open_positions(trades_path)

    # ── Fetch market data ─────────────────────────────────────
    logger.info('\nFetching market data...')
    ohlcv_data    = {}
    ofi_snapshots = {}

    # Fetch BTC first — used as relative-strength baseline in engineer_features
    # even if BTC is not in SYMBOLS
    logger.info('\n  BTC/USDT (baseline):')
    btc_df = fetch_ohlcv(exchange, 'BTC/USDT')
    if btc_df.empty:
        logger.warning('  BTC/USDT fetch failed — BTC rel-strength features will be 0')
        btc_df = None

    for sym in SYMBOLS:
        logger.info(f'\n  {sym}:')
        df = fetch_ohlcv(exchange, sym)
        if df.empty:
            continue
        ohlcv_data[sym]    = df
        ofi_snapshots[sym] = fetch_ofi_snapshot(exchange, sym)

    # ── Check exits (TP/SL/max hold) ─────────────────────────
    logger.info('\nChecking exits...')

    # Live mode: ask the exchange whether any resting stop filled
    # overnight BEFORE running check_exits()'s store+OHLCV-only logic —
    # otherwise a position the exchange already sold would get
    # re-evaluated as still open. See reconcile_stop_fills() docstring.
    reconciled_exits = []
    positions_to_check = open_positions
    if not PAPER_MODE:
        positions_to_check, reconciled_exits = reconcile_stop_fills(
            exchange, open_positions, today)
        for pos in reconciled_exits:
            balance = log_exit(trades_path, pos, balance)

    to_close = check_exits(positions_to_check, ohlcv_data, today)
    for pos in to_close:
        if PAPER_MODE:
            balance = log_exit(trades_path, pos, balance)
            continue

        # Live mode: this is a poll-driven exit (TP/trailing/max-hold),
        # not a stop fill (those were already handled above). MUST cancel
        # the resting stop before selling, or the stop can fill
        # concurrently and the position gets sold twice.
        stop_id = str(pos.get('stop_order_id', '')).strip()
        cancel_result = cancel_stop_before_exit(exchange, stop_id, pos['symbol'])

        if cancel_result['already_filled']:
            # The stop beat the poll to it — that IS the real exit.
            # Overwrite pos's theoretical exit with the stop's actual fill.
            fill_px = cancel_result['fill_price'] or (
                float(pos['entry_price']) * (1 - STOP_LOSS_PCT))
            pos = {**pos, 'exit_price': fill_px,
                  'pnl_pct': (fill_px - float(pos['entry_price'])) / float(pos['entry_price']),
                  'reason': 'SL'}
            balance = log_exit(trades_path, pos, balance)
        elif cancel_result['cancelled']:
            fill_qty = float(pos.get('fill_qty', 0) or 0)
            if fill_qty <= 0:
                logger.error(f'  {pos["symbol"]}: no fill_qty on record — '
                            f'cannot sell, manual check required')
                continue
            sold = place_live_sell(exchange, pos['symbol'], fill_qty)
            if sold:
                pos = {**pos, 'exit_price': sold['fill_price'],
                      'pnl_pct': (sold['fill_price'] - float(pos['entry_price'])) / float(pos['entry_price'])}
                balance = log_exit(trades_path, pos, balance)
            else:
                logger.error(f'  {pos["symbol"]}: sell failed after stop '
                            f'cancel — position may be open and '
                            f'UNPROTECTED (stop was just cancelled). '
                            f'Manual intervention required immediately.')
        else:
            # Neither cancelled nor confirmed filled — unresolved state.
            # Do NOT sell blind (could double-sell against a stop that's
            # about to fill) and do NOT log an exit (position may still
            # be genuinely open and protected by its still-resting stop).
            logger.error(f'  {pos["symbol"]}: stop cancel unresolved — '
                        f'skipping this exit, will retry next run')

    # FIX BUG 1: persist updated balance after exits
    save_balance(meta_path, balance)

    # ── Kill switch: re-evaluate drawdown against the updated balance ──
    # Checked here (post-exit, pre-entry) so a today's-exits loss can
    # trip the halt before any new entries are considered.
    peak_balance, should_halt, drawdown = check_kill_switch(balance, peak_balance)
    if should_halt and not halted:
        halted = True
        logger.error(f'  KILL SWITCH TRIPPED — drawdown {drawdown*100:.1f}% >= '
                     f'{KILL_SWITCH_DRAWDOWN*100:.0f}% threshold '
                     f'(balance=${balance:.2f} peak=${peak_balance:.2f}). '
                     f'New entries halted until manually reset.')
    save_kill_switch_state(meta_path, peak_balance, halted)

    # ── Generate ML signals ───────────────────────────────────
    logger.info('\nGenerating signals...')
    # closed_syms must include BOTH check_exits()'s poll-driven exits AND
    # reconcile_stop_fills()'s overnight stop fills — a symbol closed via
    # either path is no longer an active position.
    closed_syms = {p['symbol'] for p in to_close} | {p['symbol'] for p in reconciled_exits}
    active_syms = {p['symbol'] for p in open_positions
                   if p['symbol'] not in closed_syms}
    n_open      = len(active_syms)

    signals      = {}
    features_all = {}

    for sym, df in ohlcv_data.items():
        logger.info(f'\n  {sym}:')
        feats             = engineer_features(df)
        features_all[sym] = feats
        signals[sym]      = train_and_predict(feats)

    # ── Enter new positions ───────────────────────────────────
    logger.info('\nChecking entries...')
    for sym, sig in sorted(signals.items(),
                            key=lambda x: x[1].get('ensemble_prob', 0),
                            reverse=True):

        prob       = sig.get('ensemble_prob', 0)
        ml_sig     = sig.get('signal', False)
        ofi_val    = ofi_snapshots.get(sym, 0.0)
        ofi_ok     = ofi_val > OFI_GATE
        close_px   = float(ohlcv_data[sym]['close'].iloc[-1]) if sym in ohlcv_data else 0.0
        feats_row  = (features_all[sym].iloc[-1]
                      if sym in features_all else pd.Series())
        in_pos_now = sym in active_syms

        # ── Determine reject reason for signal log ────────────
        if halted:
            reject = 'KILL_SWITCH_HALTED'
            logger.info(f'  {sym}: kill switch active — new entries blocked')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=in_pos_now)
            continue

        if n_open >= MAX_POSITIONS:
            reject = 'MAX_POSITIONS'
            logger.info(f'  {sym}: max positions ({n_open}/{MAX_POSITIONS})')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=in_pos_now)
            break

        if in_pos_now:
            reject = 'ALREADY_IN_POS'
            logger.info(f'  {sym}: already open — skip')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=True)
            continue

        if sym not in ohlcv_data:
            continue

        if not ml_sig:
            reject = f'PROB_TOO_LOW_{prob:.4f}'
            logger.info(f'  {sym}: ML no signal (prob={prob:.3f})')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=False)
            continue

        if not ofi_ok:
            reject = f'OFI_NEGATIVE_{ofi_val:+.4f}'
            logger.info(f'  {sym}: ML signal but OFI={ofi_val:+.4f} blocked by gate')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=False)
            continue

        # Both ML signal AND positive OFI — enter
        entry_px   = close_px
        # Divide by MAX_POSITIONS so RISK_PER_TRADE bounds TOTAL exposure
        # across all concurrently open positions, not per-position exposure.
        # Before this fix, 3 positions open at once (RISK_PER_TRADE each,
        # sized independently against balance) could put up to
        # 3xRISK_PER_TRADE of the account at risk simultaneously -- e.g.
        # RISK_PER_TRADE=0.25 meant up to 75-100%+ of capital exposed at
        # once, not 25%, silently. Doesn't resize already-open positions
        # (no live rebalancing -- would mean selling/rebuying and adding
        # fees for no real benefit), so the guarantee holds even in the
        # worst case where all MAX_POSITIONS slots are filled.
        trade_size = balance * RISK_PER_TRADE / MAX_POSITIONS

        # FIX MINOR: minimum order check
        if trade_size < MIN_ORDER_USDT:
            logger.warning(f'  {sym}: trade_size=${trade_size:.2f} < min ${MIN_ORDER_USDT}')
            log_signal(signals_path, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False,
                       reject_reason='MIN_ORDER_SIZE',
                       in_position=False)
            continue

        logger.info(f'  ENTRY {sym} @ {entry_px:.4f} | '
                    f'prob={prob:.3f} OFI={ofi_val:+.4f} | '
                    f'TP={entry_px*(1+TAKE_PROFIT_PCT):.4f} '
                    f'SL={entry_px*(1-STOP_LOSS_PCT):.4f}')

        filled       = True
        stop_order_id = ''
        if not PAPER_MODE:
            fill = place_live_order(exchange, sym, trade_size, entry_px)
            filled = fill is not None
            if filled:
                # Use the ACTUAL fill price/qty, not the intended entry_px —
                # slippage on a taker fill can differ from the quoted price.
                entry_px = fill['fill_price']
                fill_qty = fill['fill_qty']
                sl_px    = entry_px * (1 - STOP_LOSS_PCT)
                stop_order_id = place_stop_loss_order(exchange, sym, fill_qty, sl_px)
                if not stop_order_id:
                    # FAIL SAFE: never hold a live position with no resting
                    # stop — flatten immediately rather than hope tomorrow's
                    # run catches an unprotected crash. See PROGRESS.md
                    # session 9. A real buy already executed, so either
                    # outcome below still needs a real trade-log record — this
                    # is a real (if unwanted) round-trip trade, not a no-op.
                    logger.error(f'  {sym}: stop-loss placement failed — '
                                f'flattening position immediately (fail-safe)')
                    sold = place_live_sell(exchange, sym, fill_qty)
                    if sold:
                        exit_px = sold['fill_price']
                        pnl_pct = (exit_px - entry_px) / entry_px
                        logger.error(f'  {sym}: flattened @ {exit_px:.4f} '
                                    f'(pnl={pnl_pct*100:+.2f}%) — SL placement '
                                    f'failure, not a strategy exit')
                        row_id = log_entry(trades_path, signals_path, today, sym,
                                           entry_px, sig, ofi_val, balance,
                                           feats_row, trade_size, '', fill_qty)
                        balance = log_exit(trades_path, {
                            'row_id': row_id, 'symbol': sym, 'date': today,
                            'entry_price': entry_px, 'exit_price': exit_px,
                            'pnl_pct': pnl_pct, 'hold_days': 0,
                            'reason': 'SL_PLACEMENT_FAILED',
                            'trade_size': trade_size,
                        }, balance)
                    else:
                        logger.error(f'  {sym}: FLATTEN FAILED — position may be '
                                    f'open and UNPROTECTED on the exchange. '
                                    f'Manual intervention required immediately.')
                        # Log as a held (unprotected) position so it shows up
                        # in load_open_positions() next run rather than being
                        # silently lost — better a visibly-broken row than no
                        # record of real capital sitting on the exchange.
                        log_entry(trades_path, signals_path, today, sym,
                                 entry_px, sig, ofi_val, balance, feats_row,
                                 trade_size, 'FLATTEN_FAILED', fill_qty)
                        n_open += 1
                        active_syms.add(sym)
                    filled = False   # already logged above (or intentionally
                                     # left open+unprotected) — skip the
                                     # normal log_entry below either way

        if filled:
            log_entry(trades_path, signals_path, today, sym,
                      entry_px, sig, ofi_val, balance, feats_row, trade_size,
                      stop_order_id, fill_qty if not PAPER_MODE else '')
            n_open += 1
            active_syms.add(sym)

    # ── Daily summary ─────────────────────────────────────────
    logger.info(f'\n{"="*60}')
    logger.info(f'  SUMMARY — {today}')
    logger.info(f'  Balance    : ${balance:.2f}  '
                f'(start=${STARTING_BALANCE:.2f} '
                f'PnL={balance-STARTING_BALANCE:+.2f})')
    logger.info(f'  Kill switch: {"HALTED" if halted else "ok"}  '
                f'(peak=${peak_balance:.2f} drawdown={drawdown*100:.1f}% '
                f'threshold={KILL_SWITCH_DRAWDOWN*100:.0f}%)')
    n_exits = len(to_close) + len(reconciled_exits)
    logger.info(f'  Exits      : {n_exits} | New entries: '
                f'{n_open - (len(open_positions)-n_exits)} | '
                f'Open: {n_open}/{MAX_POSITIONS}')
    logger.info(f'  Signals    :')
    for sym, sig in signals.items():
        prob   = sig.get('ensemble_prob', 0)
        ofi    = ofi_snapshots.get(sym, 0.0)
        ml_str = 'ML✓' if sig.get('signal') else 'ML✗'
        of_str = 'OFI✓' if ofi > OFI_GATE else 'OFI✗'
        act    = ' → LONG' if (sig.get('signal') and ofi > OFI_GATE) else ''
        logger.info(f'    {sym:<12} prob={prob:.3f} {ml_str} {of_str}{act}')
    fg = get_fear_greed()
    if len(fg) > 0:
        fg_val = int(fg.iloc[-1])
        fg_lbl = ('Extreme Fear' if fg_val <= 25 else 'Fear' if fg_val <= 49
                  else 'Greed' if fg_val <= 74 else 'Extreme Greed')
        logger.info(f'  Fear & Greed : {fg_val}/100 ({fg_lbl})')
    if btc_df is not None and not btc_df.empty:
        btc_ret7 = btc_df['close'].pct_change(7).iloc[-1] * 100
        logger.info(f'  BTC 7d return: {btc_ret7:+.1f}% '
                    f'({"BTC leading" if btc_ret7 > 5 else "neutral/alts leading"})')
    logger.info(f'{"="*60}')

if __name__ == '__main__':
    run()
