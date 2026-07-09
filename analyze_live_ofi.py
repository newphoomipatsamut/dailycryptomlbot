#!/usr/bin/env python3
"""
Live OFI Gate Tracker — compares real order-book OFI gate outcomes
====================================================================
Answers "does the live OFI gate filter for trade quality, or just cut
volume?" using REAL live data — not the Binance trade-flow proxy in
backtest.py. Every day the ML model wants to enter (regardless of what
the OFI gate then does) is a data point:

  - reject_reason == 'NONE'          -> OFI passed, trade actually entered.
    Real outcome comes straight from DailyTrades.
  - reject_reason starts 'OFI_NEGATIVE' -> OFI blocked it. No real trade
    exists, so the outcome is RECONSTRUCTED: fetch forward Kraken OHLCV
    from the entry day and run it through backtest.py's check_exit(),
    seeded exactly like a real entry (see run_backtest()'s entry loop).

Rows with reject_reason in {PROB_TOO_LOW_*, MAX_POSITIONS, ALREADY_IN_POS,
MIN_ORDER_SIZE} are excluded — either the ML model itself didn't want in,
or OFI was never the deciding factor (portfolio-capacity blocks happen
before/without an OFI check in some cases).

Data source: the live bot's Google Sheet isn't reachable via a local
service-account key (those are GitHub Actions secrets, not stored here)
or via OAuth in this environment. Export the DailySignals and DailyTrades
tabs to CSV manually (Google Sheets: File > Download > CSV, once per tab)
and pass both paths here.

CAVEATS — read before trusting the output:
  - Sample size is currently tiny (o(10-30) rows). Treat the comparison
    as a hint to keep watching, not a conclusion to act on. This script
    will print the exact n each run — check it before reading anything
    into the percentages.
  - The reconstruction for OFI-blocked rows uses COMPLETE historical daily
    bars (fair, matches backtest.py's convention), which is NOT the same
    as what live would have seen in real time — live's actual same-day
    check runs against a still-forming partial bar (~4hr into the UTC
    day), so a small fraction of real trades could realize differently
    than this reconstruction suggests. This mainly affects same-day exits;
    rare, and applies equally to both groups, so shouldn't bias the
    OFI-pass-vs-block comparison much.
  - reject_reason == 'NONE' rows are cross-checked against DailyTrades by
    (date, symbol); if a match isn't found (sheet export out of sync,
    trade still open, etc.) that row is dropped with a warning rather than
    silently estimated.

Usage:
    pip install ccxt pandas
    python analyze_live_ofi.py --signals "DailySignals.csv" --trades "DailyTrades.csv"
"""

import argparse
import sys

import pandas as pd
import ccxt

import backtest as bt

SYMBOLS = ['ETH/USDT', 'SOL/USDT', 'LINK/USDT']
SIGNALS_SCHEMA = [
    'date', 'symbol', 'close', 'ensemble_prob', 'rf_prob', 'xgb_prob',
    'signal_fired', 'reject_reason', 'ofi_value', 'ofi_gate_pass',
    'rsi', 'atr_pct', 'in_position',
]


def load_signals(path: str) -> pd.DataFrame:
    """
    Parse DailySignals CSV. Handles a schema change partway through the
    sheet's history: early rows (from before reject_reason/in_position
    were added to log_signal()) have only 11 fields and are dropped here
    — there are only a couple of them and they predate any OFI_NEGATIVE
    rejections anyway, so nothing useful is lost.
    """
    raw = pd.read_csv(path, header=None, skiprows=1)
    raw['nfields'] = raw.notna().sum(axis=1)
    n_legacy = (raw['nfields'] != len(SIGNALS_SCHEMA)).sum()
    if n_legacy:
        print(f'  Dropping {n_legacy} legacy-schema row(s) (pre reject_reason/in_position columns)')
    df = raw[raw['nfields'] == len(SIGNALS_SCHEMA)].copy()
    df.columns = SIGNALS_SCHEMA + ['nfields']
    df = df.drop(columns=['nfields'])
    df['date'] = pd.to_datetime(df['date']).dt.date
    df['reject_reason'] = df['reject_reason'].astype(str)
    return df


def load_trades(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df


def reconstruct_outcome(sym: str, entry_date, entry_px: float, ohlcv: dict) -> dict | None:
    """Simulate forward using backtest.py's real exit logic, entry-day-seeded."""
    df = ohlcv.get(sym)
    if df is None or entry_date not in df.index:
        return None
    entry_high = float(df.loc[entry_date, 'high'])
    pos = {
        'entry_price': entry_px,
        'entry_date': entry_date,
        'trailing_active': entry_high >= entry_px * (1 + bt.BREAKEVEN_TRIGGER),
    }
    future_dates = sorted(d for d in df.index if d > entry_date)[:bt.MAX_HOLD_DAYS + 2]
    for d in future_dates:
        result = bt.check_exit(pos, d, df.loc[d])
        if result:
            return result
    return None  # still open / ran out of forward data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--signals', required=True, help='Path to exported DailySignals CSV')
    ap.add_argument('--trades', required=True, help='Path to exported DailyTrades CSV')
    args = ap.parse_args()

    print('Loading exported sheet data...')
    signals = load_signals(args.signals)
    trades = load_trades(args.trades)

    ml_wanted = signals[
        (signals['reject_reason'] == 'NONE') |
        signals['reject_reason'].str.startswith('OFI_NEGATIVE')
    ].copy()
    n_pass = (ml_wanted['reject_reason'] == 'NONE').sum()
    n_blocked = len(ml_wanted) - n_pass
    print(f'ML-wanted-to-enter signals: {len(ml_wanted)}  '
          f'(OFI passed: {n_pass}, OFI blocked: {n_blocked})')

    if len(ml_wanted) < 30:
        print(f'\n*** SAMPLE SIZE WARNING: only {len(ml_wanted)} data points. ***')
        print('*** Treat results below as a hint, not a conclusion. ***\n')

    print('Fetching Kraken OHLCV for reconstruction...')
    exchange = ccxt.kraken({'enableRateLimit': True})
    ohlcv = {sym: bt.fetch_ohlcv_full(exchange, sym, days=720) for sym in SYMBOLS}

    rows = []
    for _, sig in ml_wanted.iterrows():
        sym, entry_date, entry_px = sig['symbol'], sig['date'], sig['close']
        group = 'OFI_PASS' if sig['reject_reason'] == 'NONE' else 'OFI_BLOCKED'

        if group == 'OFI_PASS':
            match = trades[(trades['date'] == entry_date) & (trades['symbol'] == sym)]
            if match.empty:
                print(f'  WARNING: no DailyTrades match for {entry_date} {sym} — dropping row')
                continue
            t = match.iloc[0]
            pnl_pct = (float(t['exit_price']) - float(t['entry_price'])) / float(t['entry_price'])
            rows.append({
                'date': entry_date, 'symbol': sym, 'group': group,
                'reason': t['reason'], 'pnl_pct': pnl_pct,
                'win': t['reason'] == 'TP',  # consistent with the reconstructed group's definition
            })
        else:
            outcome = reconstruct_outcome(sym, entry_date, entry_px, ohlcv)
            if outcome is None:
                print(f'  WARNING: could not reconstruct outcome for {entry_date} {sym} — dropping row')
                continue
            rows.append({
                'date': entry_date, 'symbol': sym, 'group': group,
                'reason': outcome['reason'], 'pnl_pct': outcome['pnl_pct'],
                'win': outcome['reason'] == 'TP',
            })

    result_df = pd.DataFrame(rows)
    if result_df.empty:
        print('No comparable rows found.')
        return

    print('\n' + '=' * 60)
    print('  LIVE OFI GATE — PASS vs BLOCKED (real order-book signal)')
    print('=' * 60)
    for group, grp in result_df.groupby('group'):
        n = len(grp)
        sl_rate = (grp['reason'] == 'SL').mean() * 100
        tp_rate = (grp['reason'] == 'TP').mean() * 100
        trail_rate = (grp['reason'] == 'TRAIL_BE').mean() * 100
        mean_pnl = grp['pnl_pct'].dropna().mean()
        print(f'  {group:<12} n={n:<4} TP={tp_rate:5.1f}%  SL={sl_rate:5.1f}%  '
              f'TRAIL_BE={trail_rate:5.1f}%  mean_pnl_pct={mean_pnl:+.4f}'
              if mean_pnl is not None else
              f'  {group:<12} n={n:<4} TP={tp_rate:5.1f}%  SL={sl_rate:5.1f}%  TRAIL_BE={trail_rate:5.1f}%')
    print('=' * 60)
    print(f'\n(n={len(result_df)} total — see module docstring for sample-size caveats)')
    result_df.to_csv('live_ofi_comparison.csv', index=False)
    print('Saved live_ofi_comparison.csv')


if __name__ == '__main__':
    main()
