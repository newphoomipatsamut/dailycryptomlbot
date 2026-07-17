#!/usr/bin/env python3
"""
5-Year Walk-Forward Backtest — KrakenQuant Daily ML Bot v4
==========================================================
Simulates exactly what the live bot does, day by day:
  - Same 28 features, same RF + XGB ensemble, same thresholds
  - Same TP/SL/TRAIL_BE/MAX_HOLD position logic
  - Model retrained on rolling TRAIN_WINDOW before each prediction

Deliberate differences from live:
  1. OFI gate: live uses an intraday order-book depth snapshot (OBI) as an
     entry gate. No free API provides historical order-book snapshots, so
     that exact signal can't be backtested. OFI_GATE_ENABLED (default
     False) instead gates on a PROXY: daily aggressor trade-flow imbalance
     `(2*taker_buy_vol - total_vol) / total_vol`, built from Binance kline
     data (taker_buy_base_asset_volume field). Same [-1,+1] convention,
     positive = buy pressure, but it's a different construction (full-day
     trade flow vs. an order-book depth snapshot) — treat results with the
     gate ON as informative, not equivalent to what live's OFI gate would
     have produced.
  2. FAST_MODE (default True): n_estimators=50 and retrain every 5 days.
     Set FAST_MODE=False for exact live-equivalent (200 est., daily retrain,
     ~4-5x slower — can be hours at DATA_SOURCE=binance's longer history).
  3. DATA_SOURCE (default 'binance'): live trades on Kraken, but Kraken's
     public OHLC REST endpoint hard-caps at 720 daily bars *regardless of
     the `since` param* — confirmed by requesting BTC/USD (listed on
     Kraken since 2013) at since=8y-ago and still getting only the most
     recent 720 bars. That's an API retention cap, not a listing-date
     limit (an earlier version of this comment claimed the latter —
     wrong). Binance has genuinely deeper public history for the same
     pairs (ETH from 2018, LINK from 2019, SOL from 2020 — SOL's actual
     listing date is the binding constraint, not an API cap) and is used
     here as a research-only price source. Set DATA_SOURCE='kraken' to
     backtest strictly on the live execution venue's ~2yr window instead.

Runtime: ~3-8 min (FAST_MODE=True, ~2yr Kraken), scales up with history
length and OFI fetch overhead; can be 30+ min on Binance's full range.

Usage:
    pip install ccxt pandas numpy scikit-learn xgboost
    python backtest.py
    DATA_SOURCE=kraken OFI_GATE_ENABLED=true python backtest.py
    FAST_MODE=false DATA_SOURCE=kraken python backtest.py   # exact live-equivalent
"""

import json, logging, os, time, urllib.request
from datetime import date as date_t, datetime, timezone

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SYMBOLS           = ['ETH/USDT', 'SOL/USDT', 'LINK/USDT']
RISK_PER_TRADE    = 0.01   # TOTAL portfolio risk budget, divided across up to
                           # MAX_POSITIONS concurrently open positions (see
                           # session 12 fix below) -- matches live (lowered
                           # from 0.25 in session 10, see crypto_daily_ml_v3.py
                           # for rationale). NOTE: CAGR/Sharpe/drawdown are NOT
                           # comparable to any backtest run before EITHER of
                           # these two changes — they scale with position
                           # size. Win rate/PF/trade count are still
                           # comparable (sizing-independent).
TAKE_PROFIT_PCT   = 0.030
STOP_LOSS_PCT     = 0.010
MAX_HOLD_DAYS     = 5
MAX_POSITIONS     = 3
SIGNAL_THRESHOLD  = float(os.environ.get('SIGNAL_THRESHOLD', 0.60))  # env override kept for edge-search screening, see session 13
BREAKEVEN_TRIGGER = 0.015  # once position up 1.5%, trailing SL locks in
                           # that 1.5% (not flat entry price -- see
                           # check_exit() below and PROGRESS.md session 13).
TAKER_FEE         = 0.0026   # taker fill (post v4 fix)
OFI_GATE          = 0.0      # matches live's OFI_GATE — proxy must be > this
STARTING_BALANCE  = 10_000.0

# 'binance' = deep history, research-only (live trades on Kraken).
# 'kraken'  = live's actual venue, capped at ~720 daily bars by the API.
DATA_SOURCE       = os.environ.get('DATA_SOURCE', 'binance').lower()

# Proxy trade-flow-imbalance gate (Binance-only — see module docstring).
OFI_GATE_ENABLED  = os.environ.get('OFI_GATE_ENABLED', 'false').lower() == 'true'

YEARS             = 8   # request all available; each source/symbol returns what it has
CANDLE_LIMIT      = YEARS * 366 + 30   # small buffer

TRAIN_WINDOW      = 180
MIN_WARMUP_DAYS   = 80

FAST_MODE         = os.environ.get('FAST_MODE', 'true').lower() == 'true'
                           # False → exact live-equivalent (slower). Was a
                           # hardcoded constant until 2026-07-14 — every
                           # prior "FAST_MODE=False python backtest.py"
                           # invocation in this repo's own docs/PROGRESS.md
                           # silently no-op'd and ran FAST_MODE=True instead.
N_EST             = 50  if FAST_MODE else 200
RETRAIN_EVERY     = 5   if FAST_MODE else 1   # days between model retrains

# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_ohlcv_full(exchange, symbol: str, days: int = CANDLE_LIMIT) -> pd.DataFrame:
    """
    Paginate in 720-bar pages to fetch up to `days` daily bars.
    Effective on Binance (since= is honored, pagination reaches deep history).
    On Kraken, since= is capped server-side at ~720 most-recent bars no
    matter the value passed — the loop just returns a single page there.
    """
    PAGE     = 720
    since_ms = exchange.milliseconds() - days * 86_400_000
    all_bars: list = []

    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, '1d', since=since_ms, limit=PAGE)
        except Exception as e:
            logger.warning(f'  {symbol} fetch error: {e}')
            break
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < PAGE:
            break
        since_ms = bars[-1][0] + 86_400_000   # advance past last bar
        time.sleep(0.4)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('ts').sort_values('ts')
    df['date'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.date
    df = df.drop('ts', axis=1).set_index('date')
    df = df[df['close'] > 0].dropna()
    logger.info(f'  {symbol}: {len(df)} bars  {df.index[0]} → {df.index[-1]}')
    return df


def fetch_fear_greed(limit: int = CANDLE_LIMIT) -> pd.Series:
    try:
        url = f'https://api.alternative.me/fng/?limit={limit}&format=json'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())['data']
        records = {
            pd.Timestamp(int(d['timestamp']), unit='s').date(): int(d['value'])
            for d in data
        }
        series = pd.Series(records, name='fg').sort_index()
        logger.info(f'  Fear & Greed: {len(series)} days  {series.index[0]} → {series.index[-1]}')
        return series
    except Exception as e:
        logger.warning(f'  Fear & Greed failed ({e}) — using neutral 50')
        return pd.Series(dtype=float, name='fg')


def fetch_taker_buy_ratio(binance_exchange, symbol: str, days: int) -> pd.Series:
    """
    Daily proxy for order-flow imbalance, built from Binance's raw kline
    field taker_buy_base_asset_volume (aggressive/taker BUY volume for the
    day). Value = (2*taker_buy_vol - total_vol) / total_vol, in [-1, +1],
    positive = buy pressure — same convention as the live bot's order-book
    OBI, but a different metric (day's aggregate trade flow, not an
    order-book depth snapshot). See module docstring for the caveat.
    Always queries Binance directly (raw REST via ccxt's binance client),
    regardless of DATA_SOURCE, since Kraken's OHLC data doesn't expose
    this split.
    """
    try:
        market_id = binance_exchange.market(symbol)['id']   # e.g. 'ETHUSDT'
    except Exception as e:
        logger.warning(f'  {symbol}: no Binance market for OFI proxy ({e})')
        return pd.Series(dtype=float)

    since_ms = binance_exchange.milliseconds() - days * 86_400_000
    records  = {}
    while True:
        try:
            raw = binance_exchange.publicGetKlines({
                'symbol': market_id, 'interval': '1d',
                'startTime': since_ms, 'limit': 1000,
            })
        except Exception as e:
            logger.warning(f'  {symbol}: OFI proxy fetch error ({e})')
            break
        if not raw:
            break
        for row in raw:
            d         = pd.Timestamp(int(row[0]), unit='ms', tz='UTC').date()
            total_vol = float(row[5])
            taker_buy = float(row[9])
            if total_vol > 0:
                records[d] = (2 * taker_buy - total_vol) / total_vol
        if len(raw) < 1000:
            break
        since_ms = int(raw[-1][0]) + 86_400_000
        time.sleep(0.2)

    series = pd.Series(records, name='ofi_proxy').sort_index()
    if len(series) > 0:
        logger.info(f'  {symbol}: OFI proxy {len(series)} days '
                    f'{series.index[0]} → {series.index[-1]}')
    return series


# ─── FEATURE ENGINEERING (identical to live bot) ─────────────────────────────

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
    'fg_value', 'fg_extreme_fear', 'fg_extreme_greed', 'fg_momentum',
]


def engineer_features(df: pd.DataFrame, fg: pd.Series) -> pd.DataFrame:
    """
    Precompute all features for the full OHLCV history in one pass.
    Rolling windows look backwards only — no lookahead.
    `fg` is the full Fear & Greed history; it is sliced by date alignment.
    """
    f = pd.DataFrame(index=df.index)

    for n in [1, 2, 3, 5, 10, 20]:
        f[f'ret_{n}d'] = df['close'].pct_change(n)

    f['mom_5_20'] = f['ret_5d'] - f['ret_20d']
    f['mom_sign'] = np.sign(f['ret_5d'])

    delta = df['close'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    f['rsi']           = 100 - 100 / (1 + rs)
    f['rsi_oversold']  = (f['rsi'] < 30).astype(int)
    f['rsi_overbought']= (f['rsi'] > 70).astype(int)

    hl  = df['high'] - df['low']
    hpc = (df['high'] - df['close'].shift()).abs()
    lpc = (df['low']  - df['close'].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    f['atr_pct']         = atr / df['close']
    f['atr_pct_z']       = ((f['atr_pct'] - f['atr_pct'].rolling(60).mean()) /
                             f['atr_pct'].rolling(60).std().replace(0, np.nan))
    f['high_vol_regime'] = (f['atr_pct'] >
                             f['atr_pct'].rolling(60).quantile(0.75)).astype(int)

    ema20 = df['close'].ewm(span=20, adjust=False).mean()
    ema50 = df['close'].ewm(span=50, adjust=False).mean()
    f['price_ema20_ratio'] = df['close'] / ema20 - 1
    f['price_ema50_ratio'] = df['close'] / ema50 - 1
    f['ema20_ema50_ratio']  = ema20 / ema50 - 1

    sma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    f['bb_position'] = (df['close'] - lower) / (upper - lower + 1e-10)
    f['bb_width']    = (upper - lower) / sma20

    vol_ma  = df['volume'].rolling(20).mean()
    vol_std = df['volume'].rolling(20).std()
    f['vol_z']     = (df['volume'] - vol_ma) / vol_std.replace(0, np.nan)
    f['vol_trend'] = df['volume'].pct_change(5)

    f['hl_position'] = ((df['close'] - df['low']) /
                        (df['high'] - df['low'] + 1e-10))
    f['range_pct']   = (df['high'] - df['low']) / df['close']

    f['dow'] = pd.to_datetime(f.index).dayofweek

    if len(fg) > 0:
        fg_al        = fg.reindex(pd.to_datetime(f.index).date)
        fg_al        = fg_al.ffill().fillna(50)
        fg_al.index  = f.index
        f['fg_value']        = fg_al.values / 100.0
        f['fg_extreme_fear'] = (fg_al.values <= 25).astype(int)
        f['fg_extreme_greed']= (fg_al.values >= 75).astype(int)
        fgs               = pd.Series(fg_al.values, index=f.index)
        f['fg_momentum']  = fgs.diff(7) / 100.0
    else:
        f['fg_value'] = 0.5;  f['fg_extreme_fear'] = 0
        f['fg_extreme_greed'] = 0;  f['fg_momentum'] = 0.0

    f['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    f['close']  = df['close']
    f['high']   = df['high']
    f['low']    = df['low']

    return f.dropna(subset=['ret_1d', 'rsi', 'atr_pct'])


# ─── MODEL ────────────────────────────────────────────────────────────────────

def winsorize_fit_apply(X_fit: np.ndarray, X_apply: np.ndarray,
                         lower: float = 0.01, upper: float = 0.99) -> tuple:
    """
    Clip both arrays to per-column [lower, upper] percentiles computed from
    X_fit only (no lookahead into X_apply). Caps single-day outlier rows
    (e.g. a -10% crash day) from dominating tree splits / feature scaling.
    Identical to the live bot's helper — see PROGRESS.md session 7/8.
    """
    lo = np.percentile(X_fit, lower * 100, axis=0)
    hi = np.percentile(X_fit, upper * 100, axis=0)
    return np.clip(X_fit, lo, hi), np.clip(X_apply, lo, hi)


def train_and_predict(features_slice: pd.DataFrame) -> dict:
    """
    Identical walk-forward logic to live bot:
      validate on [-20:] holdout, then retrain on full window before predicting.
    """
    # Exclude the last row (today) from training. In live, today's target is NaN
    # because tomorrow hasn't happened. Here features were precomputed on the full
    # dataset so today's target is NOT NaN — including it would be lookahead bias.
    train_df = features_slice.iloc[:-1][features_slice.iloc[:-1]['target'].notna()].copy()
    if len(train_df) < MIN_WARMUP_DAYS:
        return {'signal': False, 'ensemble_prob': 0.0, 'rf_prob': 0.0, 'xgb_prob': 0.0}

    train_df  = train_df.tail(TRAIN_WINDOW)
    X_all     = train_df[FEATURE_COLS].fillna(0).values
    y_all     = train_df['target'].values
    X_today   = features_slice[FEATURE_COLS].fillna(0).iloc[-1:].values

    split    = -20
    X_tr     = X_all[:split];  y_tr = y_all[:split]
    X_val    = X_all[split:];  y_val = y_all[split:]

    # Winsorize at 1st/99th percentile before scaling, fit on the training
    # split only (no lookahead) — caps crash-day outlier influence. See
    # PROGRESS.md session 7/8.
    X_tr, X_val = winsorize_fit_apply(X_tr, X_val)
    X_all, X_today = winsorize_fit_apply(X_all, X_today)

    val_sc   = StandardScaler()
    X_tr_sc  = val_sc.fit_transform(X_tr)
    X_val_sc = val_sc.transform(X_val)

    prod_sc  = StandardScaler()
    X_all_sc = prod_sc.fit_transform(X_all)
    X_tod_sc = prod_sc.transform(X_today)

    rf = RandomForestClassifier(
        n_estimators=N_EST, max_depth=6, min_samples_leaf=5,
        class_weight='balanced', random_state=42, n_jobs=-1,
    )
    rf.fit(X_tr_sc, y_tr)
    rf.fit(X_all_sc, y_all)
    rf_prob = float(rf.predict_proba(X_tod_sc)[0][1])

    # reg_lambda/min_child_weight/gamma curb overfitting on a 180-row/
    # 28-feature window — see PROGRESS.md session 7/8.
    if HAS_XGB:
        model = XGBClassifier(
            n_estimators=N_EST, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            reg_lambda=5.0, min_child_weight=5, gamma=0.5,
            eval_metric='logloss', random_state=42, verbosity=0,
        )
    else:
        model = GradientBoostingClassifier(
            n_estimators=N_EST, max_depth=4, learning_rate=0.05,
            subsample=0.8, random_state=42,
        )
    model.fit(X_tr_sc, y_tr)
    model.fit(X_all_sc, y_all)
    xgb_prob = float(model.predict_proba(X_tod_sc)[0][1])

    ensemble = (rf_prob + xgb_prob) / 2
    return {
        'signal':       ensemble >= SIGNAL_THRESHOLD,
        'ensemble_prob': ensemble,
        'rf_prob':      rf_prob,
        'xgb_prob':     xgb_prob,
    }


# ─── POSITION EXIT ────────────────────────────────────────────────────────────

def check_exit(pos: dict, today_dt: date_t, today_row: pd.Series) -> dict | None:
    """
    Check one open position against today's candle.
    Updates pos['trailing_active'] in place (checked BEFORE today's high).
    Returns an exit dict or None (hold).
    Caller must seed pos['trailing_active'] from the entry day's own high
    before the first call — see run_backtest()'s entry loop. Live's
    check_exits() includes the entry day in its breakeven-trigger scan;
    this function only ever sees hold_days>=1, so it can't discover that on
    its own.
    """
    entry_px   = pos['entry_price']
    entry_date = pos['entry_date']
    hold_days  = (today_dt - entry_date).days

    if hold_days == 0:
        return None   # never exit same day as entry

    daily_open = float(today_row['open'])
    daily_high = float(today_row['high'])
    daily_low  = float(today_row['low'])
    close_px   = float(today_row['close'])

    tp_price      = entry_px * (1 + TAKE_PROFIT_PCT)
    sl_price      = entry_px * (1 - STOP_LOSS_PCT)
    be_trigger_px = entry_px * (1 + BREAKEVEN_TRIGGER)

    trailing = pos.get('trailing_active', False)
    effective_sl = be_trigger_px if trailing else sl_price

    tp_hit = daily_high >= tp_price
    sl_hit = daily_low  <= effective_sl

    result = None
    if tp_hit:
        result = {'reason': 'TP',     'exit_price': tp_price,  'pnl_pct':  TAKE_PROFIT_PCT}
    elif sl_hit:
        if trailing:
            # trailing_active can only arm starting the day AFTER price
            # first touched be_trigger_px (see the update at the bottom of
            # this function) -- it is never checked against the arm day's
            # own low. Verified against real OHLCV (session 13 edge-search
            # validation): by the day this condition is confirmed, price
            # has close to always already dropped back through
            # be_trigger_px before that day's OPEN print -- 87/87 on a 2yr
            # Kraken sample, 411/414 on an 8yr Binance sample. Assuming a
            # fill AT be_trigger_px there overstates the realistically
            # achievable price by ~entry*BREAKEVEN_TRIGGER on nearly every
            # trade (confirmed this was ~96-98% of the originally reported
            # CAGR/PF/Sharpe improvement -- see PROGRESS.md session 13
            # correction). Cap at the day's open, the same way a resting
            # stop-market order would actually fill if price gapped past
            # the trigger level before the order could act on it.
            fill_px = min(be_trigger_px, daily_open)
            result = {'reason': 'TRAIL_BE', 'exit_price': fill_px,
                      'pnl_pct': (fill_px - entry_px) / entry_px}
        else:
            result = {'reason': 'SL',       'exit_price': sl_price,  'pnl_pct': -STOP_LOSS_PCT}
    elif hold_days >= MAX_HOLD_DAYS:
        pnl = (close_px - entry_px) / entry_px
        result = {'reason': f'MAX_HOLD', 'exit_price': close_px, 'pnl_pct': pnl}

    # Update trailing for future days (after exit check, so same-day logic matches live bot)
    if daily_high >= be_trigger_px:
        pos['trailing_active'] = True

    return result


# ─── METRICS ─────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[dict], equity: list[dict]) -> dict:
    if not trades:
        return {}

    eq  = pd.DataFrame(equity).set_index('date')['balance']
    ret = eq.pct_change().dropna()

    wins  = [t for t in trades if t['win']]
    loses = [t for t in trades if not t['win']]

    total_return   = (eq.iloc[-1] / eq.iloc[0]) - 1
    n_years        = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr           = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0

    rolling_max    = eq.cummax()
    drawdowns      = (eq - rolling_max) / rolling_max
    max_dd         = float(drawdowns.min())

    sharpe         = (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0
    calmar         = cagr / abs(max_dd) if max_dd != 0 else 0

    avg_win        = np.mean([t['pnl_net'] for t in wins])  if wins  else 0
    avg_loss       = np.mean([t['pnl_net'] for t in loses]) if loses else 0
    profit_factor  = (sum(t['pnl_net'] for t in wins) /
                      abs(sum(t['pnl_net'] for t in loses))) if loses else float('inf')

    return {
        'start_balance':  eq.iloc[0],
        'end_balance':    eq.iloc[-1],
        'total_return':   total_return,
        'cagr':           cagr,
        'max_drawdown':   max_dd,
        'sharpe':         sharpe,
        'calmar':         calmar,
        'n_trades':       len(trades),
        'win_rate':       len(wins) / len(trades),
        'avg_win_$':      avg_win,
        'avg_loss_$':     avg_loss,
        'profit_factor':  profit_factor,
        'avg_hold_days':  np.mean([t['hold_days'] for t in trades]),
    }


# ─── MAIN BACKTEST LOOP ───────────────────────────────────────────────────────

def run_backtest():
    logger.info('=' * 60)
    logger.info(f'  KrakenQuant Backtest — {YEARS}yr walk-forward')
    logger.info(f'  DATA_SOURCE={DATA_SOURCE}  FAST_MODE={FAST_MODE}  '
                f'n_est={N_EST}  retrain_every={RETRAIN_EVERY}d')
    logger.info(f'  OFI gate: {"ENABLED (Binance taker-buy proxy)" if OFI_GATE_ENABLED else "DISABLED"}')
    logger.info('=' * 60)

    # ── Fetch data ────────────────────────────────────────────
    logger.info('\nFetching OHLCV data...')
    exchange = ccxt.binance({'enableRateLimit': True}) if DATA_SOURCE == 'binance' \
               else ccxt.kraken({'enableRateLimit': True})

    ohlcv: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        df = fetch_ohlcv_full(exchange, sym)
        if not df.empty:
            ohlcv[sym] = df

    if not ohlcv:
        logger.error('No data fetched — exiting.')
        return

    logger.info('\nFetching Fear & Greed history...')
    fg = fetch_fear_greed()

    ofi_series: dict[str, pd.Series] = {}
    if OFI_GATE_ENABLED:
        logger.info('\nFetching OFI proxy (Binance taker-buy volume)...')
        binance_ex = exchange if DATA_SOURCE == 'binance' \
                     else ccxt.binance({'enableRateLimit': True})
        for sym in SYMBOLS:
            ofi_series[sym] = fetch_taker_buy_ratio(binance_ex, sym, CANDLE_LIMIT)

    # ── Precompute features (one pass per symbol, O(n)) ──────
    logger.info('\nEngineering features...')
    all_features: dict[str, pd.DataFrame] = {}
    for sym, df in ohlcv.items():
        all_features[sym] = engineer_features(df, fg)
        logger.info(f'  {sym}: {len(all_features[sym])} feature rows')

    # ── Walk-forward loop ─────────────────────────────────────
    all_dates = sorted({d for df in ohlcv.values() for d in df.index})
    logger.info(f'\nWalk-forward: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} days)\n')

    balance     = STARTING_BALANCE
    open_pos: dict[str, dict] = {}   # sym → position dict
    trades: list[dict]  = []
    equity: list[dict]  = []

    # Cache last signal per symbol so we don't retrain every single day
    sig_cache: dict[str, tuple] = {}   # sym → (last_retrain_date, signal_dict)

    for d_idx, today_dt in enumerate(all_dates):
        # ── 1. Check exits ────────────────────────────────────
        for sym in list(open_pos.keys()):
            if sym not in ohlcv or today_dt not in ohlcv[sym].index:
                continue
            row    = ohlcv[sym].loc[today_dt]
            result = check_exit(open_pos[sym], today_dt, row)
            if result:
                pos        = open_pos.pop(sym)
                trade_size = pos['trade_size']
                pnl_gross  = result['pnl_pct'] * trade_size
                fees       = trade_size * TAKER_FEE * 2
                pnl_net    = pnl_gross - fees
                balance   += pnl_net
                trades.append({
                    'symbol':      sym,
                    'entry_date':  pos['entry_date'],
                    'exit_date':   today_dt,
                    'entry_price': pos['entry_price'],
                    'exit_price':  result['exit_price'],
                    'hold_days':   (today_dt - pos['entry_date']).days,
                    'reason':      result['reason'],
                    'trade_size':  trade_size,
                    'pnl_gross':   round(pnl_gross, 4),
                    'pnl_net':     round(pnl_net, 4),
                    'fees':        round(fees, 4),
                    'win':         pnl_net > 0,
                    'signal_prob': pos.get('signal_prob', 0),
                })

        equity.append({'date': today_dt, 'balance': balance})

        # ── 2. Generate signals and enter ─────────────────────
        if len(open_pos) >= MAX_POSITIONS:
            continue

        candidates = []
        for sym in SYMBOLS:
            if sym in open_pos or sym not in all_features:
                continue
            feats = all_features[sym]
            feats_slice = feats[feats.index <= today_dt]
            if len(feats_slice) < MIN_WARMUP_DAYS:
                continue

            # Use cached signal unless it's time to retrain
            cache = sig_cache.get(sym)
            if cache and (today_dt - cache[0]).days < RETRAIN_EVERY:
                sig = cache[1]
            else:
                sig = train_and_predict(feats_slice)
                sig_cache[sym] = (today_dt, sig)

            candidates.append((sym, sig, float(feats_slice['close'].iloc[-1])))

        # Sort highest-probability first
        candidates.sort(key=lambda x: x[1]['ensemble_prob'], reverse=True)

        for sym, sig, entry_px in candidates:
            if len(open_pos) >= MAX_POSITIONS:
                break
            if not sig['signal']:
                continue
            if OFI_GATE_ENABLED:
                ofi_today = ofi_series.get(sym, pd.Series()).get(today_dt, 0.0)
                if ofi_today <= OFI_GATE:
                    continue
            # Divide by MAX_POSITIONS so RISK_PER_TRADE bounds TOTAL exposure
            # across all concurrently open positions -- matches the same fix
            # in crypto_daily_ml_v3.py (kept in sync per repo convention).
            # Before this fix, RISK_PER_TRADE was applied per-position with
            # no division, so up to MAX_POSITIONS x RISK_PER_TRADE of the
            # account could be at risk simultaneously, silently.
            trade_size = balance * RISK_PER_TRADE / MAX_POSITIONS
            if trade_size < 15:
                continue
            # Live's check_exits() scans entry_date..yesterday (inclusive of the
            # entry day itself) for the breakeven trigger, unlike check_exit()
            # below which only evaluates hold_days>=1. Seed trailing_active here
            # from the entry day's own high so backtest matches live exactly.
            entry_high = float(ohlcv[sym].loc[today_dt, 'high'])
            entry_trailing = entry_high >= entry_px * (1 + BREAKEVEN_TRIGGER)
            open_pos[sym] = {
                'entry_price':    entry_px,
                'entry_date':     today_dt,
                'trade_size':     trade_size,
                'trailing_active': entry_trailing,
                'signal_prob':    sig['ensemble_prob'],
            }

        # Progress log every 180 days
        if (d_idx + 1) % 180 == 0:
            pct = (d_idx + 1) / len(all_dates) * 100
            logger.info(f'  {today_dt}  bal=${balance:,.2f}  '
                        f'trades={len(trades)}  open={len(open_pos)}  [{pct:.0f}%]')

    # Close any remaining open positions at last known price
    for sym, pos in open_pos.items():
        if sym not in ohlcv:
            continue
        last_row  = ohlcv[sym].iloc[-1]
        close_px  = float(last_row['close'])
        pnl_pct   = (close_px - pos['entry_price']) / pos['entry_price']
        trade_size = pos['trade_size']
        pnl_gross  = pnl_pct * trade_size
        fees       = trade_size * TAKER_FEE * 2
        pnl_net    = pnl_gross - fees
        balance   += pnl_net
        trades.append({
            'symbol':      sym,
            'entry_date':  pos['entry_date'],
            'exit_date':   all_dates[-1],
            'entry_price': pos['entry_price'],
            'exit_price':  close_px,
            'hold_days':   (all_dates[-1] - pos['entry_date']).days,
            'reason':      'FINAL_CLOSE',
            'trade_size':  trade_size,
            'pnl_gross':   round(pnl_gross, 4),
            'pnl_net':     round(pnl_net, 4),
            'fees':        round(fees, 4),
            'win':         pnl_net > 0,
            'signal_prob': pos.get('signal_prob', 0),
        })

    # ── Results ───────────────────────────────────────────────
    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity)

    suffix = f'{DATA_SOURCE}{"_ofi" if OFI_GATE_ENABLED else ""}'
    trades_file = f'backtest_trades_{suffix}.csv'
    equity_file = f'backtest_equity_{suffix}.csv'
    trades_df.to_csv(trades_file, index=False)
    equity_df.to_csv(equity_file, index=False)
    logger.info(f'\nSaved {trades_file} and {equity_file}')

    m = compute_metrics(trades, equity)
    if not m:
        logger.info('No trades generated.')
        return

    print('\n' + '=' * 55)
    print(f'  BACKTEST RESULTS  ({all_dates[0]} → {all_dates[-1]})')
    print('=' * 55)
    print(f'  Starting balance   ${m["start_balance"]:>10,.2f}')
    print(f'  Ending balance     ${m["end_balance"]:>10,.2f}')
    print(f'  Total return       {m["total_return"]*100:>10.1f}%')
    print(f'  CAGR               {m["cagr"]*100:>10.1f}%')
    print(f'  Max drawdown       {m["max_drawdown"]*100:>10.1f}%')
    print(f'  Sharpe (ann.)      {m["sharpe"]:>10.2f}')
    print(f'  Calmar             {m["calmar"]:>10.2f}')
    print('-' * 55)
    print(f'  Total trades       {m["n_trades"]:>10}')
    print(f'  Win rate           {m["win_rate"]*100:>10.1f}%')
    print(f'  Avg win            ${m["avg_win_$"]:>10.2f}')
    print(f'  Avg loss           ${m["avg_loss_$"]:>10.2f}')
    print(f'  Profit factor      {m["profit_factor"]:>10.2f}')
    print(f'  Avg hold days      {m["avg_hold_days"]:>10.1f}')
    print('-' * 55)

    if not trades_df.empty:
        print('\n  Per-symbol breakdown:')
        for sym in SYMBOLS:
            st = trades_df[trades_df['symbol'] == sym]
            if st.empty:
                print(f'    {sym:<12}  no trades')
                continue
            wr  = st['win'].mean() * 100
            pnl = st['pnl_net'].sum()
            n   = len(st)
            print(f'    {sym:<12}  {n:>3} trades  win={wr:.0f}%  net PnL=${pnl:+.2f}')

    print('\n  Exit reason breakdown:')
    for reason, grp in trades_df.groupby('reason'):
        net = grp['pnl_net'].sum()
        print(f'    {reason:<14}  {len(grp):>3}x  net=${net:+.2f}')

    if OFI_GATE_ENABLED:
        print('\n  NOTE: OFI gate ON — Binance taker-buy-volume PROXY, not an')
        print('        order-book snapshot like live uses. See module docstring.')
    else:
        print('\n  NOTE: OFI gate disabled — upper-bound estimate.')
    print(f'  NOTE: price data from {DATA_SOURCE.upper()}'
          f'{" (research only — live trades on Kraken)" if DATA_SOURCE == "binance" else " (matches live venue)"}.')
    if FAST_MODE:
        print('  NOTE: FAST_MODE on (n_est=50, retrain every 5d).')
        print('        Run with FAST_MODE=False for exact live-equivalent.')
    print('=' * 55 + '\n')


if __name__ == '__main__':
    run_backtest()
