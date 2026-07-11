"""
KrakenQuant Daily ML Bot — v3
==============================
Daily timeframe crypto trading using Random Forest + XGBoost ensemble.

Why daily vs tick scalping:
  - Tick scalping: fee = 0.32% vs avg move = 0.02%  → fee is 1400% of edge
  - Daily signals: fee = 0.32% vs avg move = 2-4%   → fee is 8-16% of edge
  - OFI correctly predicted direction 92% on tick data
  - On daily bars, RSI + ATR + EMA + momentum = strong signal set

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
  - Trade size: 25% of balance per signal
  - Max 3 concurrent positions (one per symbol)
  - TP: 3%  SL: 1%  Max hold: 5 days

See backtest.py for measured performance (~10.4% CAGR over the ~2yr history
Kraken has for these USDT pairs, OFI gate disabled — an upper-bound estimate,
not a live-equivalent number). Treat any "expected return" figure as
unverified until backed by a FAST_MODE=False backtest run.

Setup:
  pip install ccxt pandas numpy scikit-learn xgboost gspread google-auth

Environment variables:
  KRAKEN_API_KEY, KRAKEN_SECRET, GOOGLE_CREDS_JSON, GOOGLE_SHEET_ID
  PAPER_MODE  ('true' default), PAPER_BALANCE (default 10000)
"""

import os, json, logging, urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import ccxt
import gspread
from google.oauth2.service_account import Credentials
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
RISK_PER_TRADE   = 0.25          # raised from 0.10 — validated by backtest
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

TRAIN_WINDOW     = 180
MIN_WARMUP_DAYS  = 80            # raised from 60 — ensures 60 train + 20 val
CANDLE_LIMIT     = 365

# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def init_sheets():
    try:
        creds_json = os.environ.get('GOOGLE_CREDS_JSON')
        if not creds_json:
            logger.warning('GOOGLE_CREDS_JSON not set — Sheets disabled')
            return None, None, None, None

        creds  = Credentials.from_service_account_info(
                     json.loads(creds_json), scopes=SCOPES)
        client = gspread.authorize(creds)
        ss     = client.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))

        def get_or_create(name, rows, cols, headers):
            try:
                return ss.worksheet(name)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(name, rows=rows, cols=cols)
                ws.append_row(headers)
                return ws

        trades_ws  = get_or_create('DailyTrades', 5000, 17, [
            'row_id', 'date', 'symbol', 'action', 'entry_price', 'exit_price',
            'pnl_gross', 'pnl_net', 'fees', 'reason',
            'hold_days', 'win', 'balance_after',
            'signal_prob', 'rf_prob', 'xgb_prob', 'trade_size',
        ])
        signals_ws = get_or_create('DailySignals', 50000, 13, [
            'date', 'symbol', 'close', 'ensemble_prob', 'rf_prob', 'xgb_prob',
            'signal_fired', 'reject_reason', 'ofi_value', 'ofi_gate_pass',
            'rsi', 'atr_pct', 'in_position',
        ])
        # FIX BUG 1: Meta tab stores persistent balance
        meta_ws    = get_or_create('DailyMeta', 100, 3, [
            'key', 'value', 'updated'
        ])

        logger.info('Google Sheets connected')
        return client, trades_ws, signals_ws, meta_ws

    except Exception as e:
        logger.error(f'Sheets init failed: {e}')
        return None, None, None, None

# ─────────────────────────────────────────────────────────────
# FIX BUG 1: PERSISTENT BALANCE
# ─────────────────────────────────────────────────────────────
def load_balance(meta_ws) -> float:
    """Load current balance from Sheets Meta tab. Falls back to STARTING_BALANCE."""
    if meta_ws is None:
        return STARTING_BALANCE
    try:
        rows = meta_ws.get_all_records()
        for row in rows:
            if row.get('key') == 'balance':
                val = float(row['value'])
                logger.info(f'Loaded balance from Sheets: ${val:.2f}')
                return val
        # First run — initialise
        meta_ws.append_row(['balance', str(STARTING_BALANCE),
                             datetime.now(timezone.utc).isoformat()])
        logger.info(f'Balance initialised: ${STARTING_BALANCE:.2f}')
        return STARTING_BALANCE
    except Exception as e:
        logger.error(f'Load balance failed: {e} — using STARTING_BALANCE')
        return STARTING_BALANCE

def save_balance(meta_ws, balance: float) -> None:
    """Persist current balance to Sheets Meta tab."""
    if meta_ws is None:
        return
    try:
        rows  = meta_ws.get_all_values()
        today = datetime.now(timezone.utc).isoformat()
        for i, row in enumerate(rows):
            if row and row[0] == 'balance':
                meta_ws.update(range_name=f'A{i+1}:C{i+1}',
                               values=[['balance', str(round(balance, 4)), today]])
                return
        meta_ws.append_row(['balance', str(round(balance, 4)), today])
    except Exception as e:
        logger.error(f'Save balance failed: {e}')

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
def load_open_positions(trades_ws) -> list:
    """
    Load open positions from Sheets.
    FIX MINOR: only scans last 90 days (not entire sheet history).
    """
    if trades_ws is None:
        return []
    try:
        rows     = trades_ws.get_all_records()
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
def _next_row_id(trades_ws) -> str:
    """Generate a unique row ID for each trade entry."""
    try:
        n = len(trades_ws.get_all_values())
        return f'T{n:04d}'
    except Exception:
        return f'T{int(datetime.now().timestamp())}'

def log_signal(signals_ws, today: str, symbol: str,
               close_px: float, signal: dict,
               ofi_val: float, features_row: pd.Series,
               signal_fired: bool, reject_reason: str,
               in_position: bool) -> None:
    """
    Log every symbol's daily signal to DailySignals — even when no trade fires.
    This is the rejection log: shows WHY each symbol didn't trade each day.
    reject_reason values:
      NONE              — signal fired, trade entered
      PROB_TOO_LOW      — ensemble prob below SIGNAL_THRESHOLD
      OFI_NEGATIVE      — ML signal but OFI gate blocked entry
      ALREADY_IN_POS    — already holding this symbol
      MAX_POSITIONS     — at max concurrent positions
      INSUFFICIENT_DATA — not enough bars to train
    """
    if signals_ws is None:
        return
    try:
        signals_ws.append_row([
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
        ], value_input_option='RAW')
    except Exception as e:
        logger.error(f'Log signal failed: {e}')


def log_entry(trades_ws, signals_ws, today: str, symbol: str,
              entry_price: float, signal: dict,
              ofi_val: float, balance: float,
              features_row: pd.Series, trade_size: float) -> str:
    """Log new trade entry to DailyTrades. Returns row_id for later exit update."""
    row_id = _next_row_id(trades_ws) if trades_ws else 'T0000'

    if trades_ws:
        try:
            trades_ws.append_row([
                row_id, today, symbol, 'OPEN',
                round(entry_price, 6), '',
                '', '', '', '',
                '', '', round(balance, 2),
                round(signal['ensemble_prob'], 4),
                round(signal['rf_prob'], 4),
                round(signal['xgb_prob'], 4),
                round(trade_size, 4),             # col Q — used by log_exit
            ], value_input_option='RAW')
        except Exception as e:
            logger.error(f'Log entry failed: {e}')

    # Signal log: fired = True, no reject reason
    log_signal(signals_ws, today, symbol, entry_price, signal,
               ofi_val, features_row,
               signal_fired=True, reject_reason='NONE',
               in_position=False)

    return row_id

def log_exit(trades_ws, pos: dict, balance: float) -> float:
    """
    Log trade exit. Returns updated balance.
    Uses trade_size stored at entry (col Q) so PnL is calculated on the
    actual allocated amount, not the current balance.
    """
    stored = pos.get('trade_size', '')
    trade_size = float(stored) if stored else balance * RISK_PER_TRADE
    pnl_gross  = pos['pnl_pct'] * trade_size
    fees       = trade_size * TAKER_FEE * 2
    pnl_net    = pnl_gross - fees
    win        = pnl_net > 0
    new_bal    = balance + pnl_net

    logger.info(f'  CLOSED {pos["symbol"]} [{pos["reason"]}] | '
                f'gross=${pnl_gross:+.2f} fee=${fees:.2f} '
                f'net=${pnl_net:+.2f} bal=${new_bal:.2f}')

    if trades_ws:
        try:
            rows    = trades_ws.get_all_values()
            row_id  = str(pos.get('row_id', ''))
            for i, row in enumerate(rows):
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
                    trades_ws.update(range_name=f'A{i+1}:Q{i+1}', values=[[
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
                    ]])
                    break
        except Exception as e:
            logger.error(f'Log exit failed: {e}')

    return new_bal

# ─────────────────────────────────────────────────────────────
# LIVE ORDER (with fill confirmation)
# ─────────────────────────────────────────────────────────────
def place_live_order(exchange, symbol: str, trade_size: float,
                     entry_px: float) -> bool:
    """
    Place limit buy with 60s fill timeout. No post-only flag so the order
    fills immediately as a taker if price has moved — avoids being rejected
    on the breakouts where the ML signal fires.
    Returns True if filled, False otherwise.
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
                fill_px = float(status.get('average', entry_px))
                logger.info(f'  FILLED @ {fill_px:.4f}')
                return True
            if status['status'] == 'canceled':
                logger.warning(f'  Order {order_id} was cancelled')
                return False

        # Timeout — cancel
        exchange.cancel_order(order_id, symbol)
        logger.warning(f'  Order {order_id} timed out — cancelled')
        return False

    except Exception as e:
        logger.error(f'  Live order failed: {e}')
        return False

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
    _, trades_ws, signals_ws, meta_ws = init_sheets()

    # FIX BUG 1: load balance from Sheets
    balance = load_balance(meta_ws)
    logger.info(f'Balance loaded: ${balance:.2f}')

    exchange = ccxt.kraken({
        'apiKey':          os.environ.get('KRAKEN_API_KEY', ''),
        'secret':          os.environ.get('KRAKEN_SECRET', ''),
        'enableRateLimit': True,
    })

    # ── Load open positions ────────────────────────────────────
    open_positions = load_open_positions(trades_ws)

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
    to_close = check_exits(open_positions, ohlcv_data, today)
    for pos in to_close:
        balance = log_exit(trades_ws, pos, balance)

    # FIX BUG 1: persist updated balance after exits
    save_balance(meta_ws, balance)

    # ── Generate ML signals ───────────────────────────────────
    logger.info('\nGenerating signals...')
    closed_syms = {p['symbol'] for p in to_close}
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
        if n_open >= MAX_POSITIONS:
            reject = 'MAX_POSITIONS'
            logger.info(f'  {sym}: max positions ({n_open}/{MAX_POSITIONS})')
            log_signal(signals_ws, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=in_pos_now)
            break

        if in_pos_now:
            reject = 'ALREADY_IN_POS'
            logger.info(f'  {sym}: already open — skip')
            log_signal(signals_ws, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=True)
            continue

        if sym not in ohlcv_data:
            continue

        if not ml_sig:
            reject = f'PROB_TOO_LOW_{prob:.4f}'
            logger.info(f'  {sym}: ML no signal (prob={prob:.3f})')
            log_signal(signals_ws, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=False)
            continue

        if not ofi_ok:
            reject = f'OFI_NEGATIVE_{ofi_val:+.4f}'
            logger.info(f'  {sym}: ML signal but OFI={ofi_val:+.4f} blocked by gate')
            log_signal(signals_ws, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False, reject_reason=reject,
                       in_position=False)
            continue

        # Both ML signal AND positive OFI — enter
        entry_px   = close_px
        trade_size = balance * RISK_PER_TRADE

        # FIX MINOR: minimum order check
        if trade_size < MIN_ORDER_USDT:
            logger.warning(f'  {sym}: trade_size=${trade_size:.2f} < min ${MIN_ORDER_USDT}')
            log_signal(signals_ws, today, sym, close_px, sig, ofi_val,
                       feats_row, signal_fired=False,
                       reject_reason='MIN_ORDER_SIZE',
                       in_position=False)
            continue

        logger.info(f'  ENTRY {sym} @ {entry_px:.4f} | '
                    f'prob={prob:.3f} OFI={ofi_val:+.4f} | '
                    f'TP={entry_px*(1+TAKE_PROFIT_PCT):.4f} '
                    f'SL={entry_px*(1-STOP_LOSS_PCT):.4f}')

        filled = True
        if not PAPER_MODE:
            filled = place_live_order(exchange, sym, trade_size, entry_px)

        if filled:
            log_entry(trades_ws, signals_ws, today, sym,
                      entry_px, sig, ofi_val, balance, feats_row, trade_size)
            n_open += 1
            active_syms.add(sym)

    # ── Daily summary ─────────────────────────────────────────
    logger.info(f'\n{"="*60}')
    logger.info(f'  SUMMARY — {today}')
    logger.info(f'  Balance    : ${balance:.2f}  '
                f'(start=${STARTING_BALANCE:.2f} '
                f'PnL={balance-STARTING_BALANCE:+.2f})')
    logger.info(f'  Exits      : {len(to_close)} | New entries: '
                f'{n_open - (len(open_positions)-len(to_close))} | '
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
