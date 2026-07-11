#!/usr/bin/env python3
"""
Forward test — does live paper performance validate the backtest's edge?
========================================================================
The go/no-go gate before any real capital. Compares three things over the
SAME calendar window (the live paper-trading period):

  1. FIDELITY  — for every trade the live bot actually recorded, reconstruct
     what the strategy's exit logic (backtest.check_exit, entry-day-seeded
     exactly like run_backtest) says the outcome SHOULD have been on real
     Kraken OHLCV, and compare to the sheet's recorded reason. Mismatches
     mean either a bookkeeping bug or the known partial-bar timing artifact
     (live checks ~4h into the UTC day). Extends the session-3 n=8 check to
     ALL closed trades.

  2. AGGREGATES — live realized (from DailyTrades) vs backtest-predicted
     (backtest_trades_<src>.csv sliced to the live window). The backtest
     runs WITHOUT the real OFI gate, so it takes MORE trades than live —
     that gap is the OFI gate's volume effect, noted explicitly.

  3. BENCHMARK — equal-weight buy-and-hold of ETH/SOL/LINK over the same
     window. If the strategy doesn't beat dumb holding, there's no edge to
     validate.

Honesty layer: prints n at every step. With o(10-30) live trades, nothing
here is statistically conclusive — it's "consistent with an edge" or "not,"
read alongside the sample-size caveat. Do NOT treat a good forward test as
proof the strategy works; treat a BAD one as proof it doesn't.

Data source: DailyTrades isn't reachable locally (creds are GH Actions
secrets). Export it manually (Google Sheets > File > Download > CSV) and
pass the path via --trades.

Usage:
    python forward_test.py --trades DailyTrades.csv
    # optionally point at a non-default backtest run:
    python forward_test.py --trades DailyTrades.csv --src kraken
"""

import argparse
import sys
from datetime import date

import numpy as np
import pandas as pd
import ccxt

import backtest as bt

SYMBOLS = bt.SYMBOLS
STARTING_BALANCE = bt.STARTING_BALANCE


# ─── live DailyTrades (manual CSV export) ────────────────────────────────────
def load_live_trades(path: str) -> pd.DataFrame:
    """
    Parse exported DailyTrades CSV. Canonical 17-col schema:
      row_id, date, symbol, action, entry_price, exit_price, pnl_gross,
      pnl_net, fees, reason, hold_days, win, balance_after,
      ensemble_prob, rf_prob, xgb_prob, trade_size
    Robust to extra/missing trailing cols; only needs the core fields.
    """
    df = pd.read_csv(path)
    needed = ['date', 'symbol', 'action', 'entry_price', 'exit_price',
              'pnl_net', 'reason', 'balance_after']
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f'ERROR: DailyTrades CSV missing columns {missing}')
        print(f'       found columns: {list(df.columns)}')
        sys.exit(1)
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.date
    df = df[df['date'].notna()].copy()
    for c in ['entry_price', 'exit_price', 'pnl_net', 'balance_after']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


# ─── fidelity: reconstruct each live trade's expected outcome ───────────────
def reconstruct(sym, entry_date, entry_px, ohlcv):
    """Run real exit logic forward from entry, entry-day-seeded (matches live)."""
    df = ohlcv.get(sym)
    if df is None or entry_date not in df.index:
        return None
    entry_high = float(df.loc[entry_date, 'high'])
    pos = {
        'entry_price': entry_px,
        'entry_date': entry_date,
        'trailing_active': entry_high >= entry_px * (1 + bt.BREAKEVEN_TRIGGER),
    }
    future = sorted(d for d in df.index if d > entry_date)[:bt.MAX_HOLD_DAYS + 2]
    for d in future:
        res = bt.check_exit(pos, d, df.loc[d])
        if res:
            return res
    return None


def fidelity_report(closed, ohlcv):
    rows = []
    for _, t in closed.iterrows():
        rec_reason = str(t['reason'])
        rec_pnl = float(t['pnl_net']) if pd.notna(t['pnl_net']) else None
        out = reconstruct(t['symbol'], t['date'], float(t['entry_price']), ohlcv)
        if out is None:
            rows.append((t['date'], t['symbol'], rec_reason, 'NO_DATA', False))
            continue
        exp_reason = out['reason']
        match = rec_reason == exp_reason
        rows.append((t['date'], t['symbol'], rec_reason, exp_reason, match))
    n = len(rows)
    n_match = sum(1 for *_, m in rows if m)
    mism = [(d, s, r, e) for d, s, r, e, m in rows if not m]
    return n, n_match, mism


# ─── aggregate stats ─────────────────────────────────────────────────────────
def aggregates(trades_df, equity_balance_series, label):
    """trades_df: has 'pnl_net','reason'. equity: pandas Series of balance by date."""
    n = len(trades_df)
    if n == 0:
        return {'label': label, 'n': 0}
    wins = trades_df[trades_df['pnl_net'] > 0]
    losses = trades_df[trades_df['pnl_net'] <= 0]
    gross_win = wins['pnl_net'].sum()
    gross_loss = abs(losses['pnl_net'].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    total_pnl = trades_df['pnl_net'].sum()
    ret = total_pnl / STARTING_BALANCE
    m = {}
    m['label'] = label
    m['n'] = n
    m['total_return'] = ret
    m['win_rate'] = len(wins) / n
    m['profit_factor'] = pf
    m['avg_pnl_pct'] = (trades_df['pnl_net'] / STARTING_BALANCE).mean() * 100
    tp = (trades_df['reason'] == 'TP').sum()
    sl = (trades_df['reason'] == 'SL').sum()
    trail = trades_df['reason'].astype(str).str.startswith('TRAIL').sum()
    mh = trades_df['reason'].astype(str).str.startswith('MAX_HOLD').sum()
    m['tp'] = int(tp); m['sl'] = int(sl); m['trail'] = int(trail); m['maxhold'] = int(mh)
    # max drawdown from the balance series
    if equity_balance_series is not None and len(equity_balance_series) > 1:
        eq = equity_balance_series.sort_index()
        roll = eq.cummax()
        dd = (eq - roll) / roll
        m['max_dd'] = float(dd.min())
    else:
        m['max_dd'] = None
    return m


def fmt(m):
    if m['n'] == 0:
        return f"  {m['label']:<28} n=0  (no trades)"
    dd = f"{m['max_dd']*100:.1f}%" if m['max_dd'] is not None else "n/a"
    pf = f"{m['profit_factor']:.2f}" if m['profit_factor'] != float('inf') else "inf"
    return (f"  {m['label']:<28} n={m['n']:<4} ret={m['total_return']*100:+6.1f}%  "
            f"win={m['win_rate']*100:4.0f}%  PF={pf:>5}  "
            f"TP={m['tp']} SL={m['sl']} TRAIL={m['trail']} MAXH={m['maxhold']}  DD={dd}")


# ─── buy & hold benchmark (equal weight) ─────────────────────────────────────
def buy_hold(ohlcv, window_start, window_end):
    rets = []
    for sym in SYMBOLS:
        df = ohlcv.get(sym)
        if df is None or df.empty:
            continue
        seg = df.loc[(df.index >= window_start) & (df.index <= window_end)]
        if len(seg) < 2:
            continue
        rets.append(seg['close'].iloc[-1] / seg['close'].iloc[0] - 1)
    if not rets:
        return None
    return float(np.mean(rets))


# ─── backtest-predicted slice ────────────────────────────────────────────────
def load_backtest_slice(src, window_start, window_end):
    tf = f'backtest_trades_{src}.csv'
    ef = f'backtest_equity_{src}.csv'
    try:
        bt_trades = pd.read_csv(tf)
    except FileNotFoundError:
        print(f'  (no {tf} — run `DATA_SOURCE={src} python backtest.py` first)')
        return None, None
    bt_trades['entry_date'] = pd.to_datetime(bt_trades['entry_date']).dt.date
    sl = bt_trades[(bt_trades['entry_date'] >= window_start) &
                   (bt_trades['entry_date'] <= window_end)].copy()
    try:
        bt_eq = pd.read_csv(ef)
        bt_eq['date'] = pd.to_datetime(bt_eq['date']).dt.date
        bt_eq = bt_eq.set_index('date')['balance'].sort_index()
        seg = bt_eq.loc[(bt_eq.index >= window_start) & (bt_eq.index <= window_end)]
        eq = seg if len(seg) > 1 else None
    except FileNotFoundError:
        eq = None
    return sl, eq


# ─── main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trades', required=True, help='Exported DailyTrades CSV')
    ap.add_argument('--src', default='kraken',
                    help='Backtest data source suffix to compare against (default kraken)')
    args = ap.parse_args()

    print('Loading live DailyTrades...')
    live = load_live_trades(args.trades)
    closed = live[live['action'].astype(str).str.upper() == 'CLOSED'].copy()
    still_open = live[~live.index.isin(closed.index)]
    if len(closed) == 0:
        print('No CLOSED trades found in the export.')
        return

    window_start = closed['date'].min()
    window_end = closed['date'].max()
    print(f'  {len(closed)} closed trade(s), {len(still_open)} still OPEN')
    print(f'  Live window: {window_start} -> {window_end}')

    print('\nFetching Kraken OHLCV (fidelity + benchmark)...')
    exchange = ccxt.kraken({'enableRateLimit': True})
    ohlcv = {sym: bt.fetch_ohlcv_full(exchange, sym, days=720) for sym in SYMBOLS}

    # ── 1. FIDELITY ──────────────────────────────────────────────────────────
    print('\n' + '=' * 72)
    print('  1. FIDELITY — recorded exit reason vs strategy logic on real prices')
    print('=' * 72)
    n, n_match, mism = fidelity_report(closed, ohlcv)
    print(f'  {n_match}/{n} live trades match what check_exit() reconstructs '
          f'({n_match/n*100:.0f}%)')
    if mism:
        print(f'  Mismatches ({len(mism)}) — usually same-day-exit partial-bar cases:')
        for d, s, rec, exp in mism:
            print(f'    {d} {s}: recorded={rec}  reconstructed={exp}')

    # ── 2. AGGREGATES ────────────────────────────────────────────────────────
    print('\n' + '=' * 72)
    print('  2. AGGREGATES over the live window (realized vs predicted)')
    print('=' * 72)

    # live equity curve from balance_after
    live_eq = closed.sort_values('date').set_index('date')['balance_after']

    live_m = aggregates(closed, live_eq, 'LIVE (realized)')
    bt_sl, bt_eq = load_backtest_slice(args.src, window_start, window_end)
    bt_m = aggregates(bt_sl, bt_eq, f'BACKTEST {args.src} (predicted)') if bt_sl is not None else None

    print(fmt(live_m))
    if bt_m:
        print(fmt(bt_m))
        # fair comparison: backtest filtered to the SAME trades live took
        if bt_sl is not None:
            keys = set(zip(closed['date'], closed['symbol']))
            matched = bt_sl[bt_sl.apply(
                lambda r: (r['entry_date'], r['symbol']) in keys, axis=1)]
            if len(matched) > 0:
                # backtest's predicted outcome for exactly live's trade set
                m_m = aggregates(matched, None, f'  └ backtest on live\'s trades only')
                print(fmt(m_m))
    else:
        print(f'  (run `DATA_SOURCE={args.src} python backtest.py` to add predicted side)')

    # ── 3. BENCHMARK ─────────────────────────────────────────────────────────
    bh = buy_hold(ohlcv, window_start, window_end)
    print('\n' + '=' * 72)
    print('  3. BENCHMARK — equal-weight buy & hold ETH/SOL/LINK, same window')
    print('=' * 72)
    if bh is not None:
        print(f'  Buy & hold avg return: {bh*100:+.1f}%   '
              f'(strategy live: {live_m["total_return"]*100:+.1f}%)')
        edge = live_m['total_return'] - bh
        print(f'  Strategy edge vs hold: {edge*100:+.1f}pp')
    else:
        print('  (no benchmark data for window)')

    # ── sample-size honesty ──────────────────────────────────────────────────
    print('\n' + '=' * 72)
    print('  SAMPLE-SIZE READ')
    print('=' * 72)
    print(f'  n = {len(closed)} closed live trades. '
          f'{"BELOW the ~30 minimum for any conclusion." if len(closed) < 30 else "Adequate for a tentative read."}')
    print('  A matching forward test means "not falsified," NOT "proven."')
    print('  Live capital still requires: resting stop orders, drawdown kill-switch,')
    print('  sane sizing — none of which exist yet (see forward_test docstring).')
    print('=' * 72)


if __name__ == '__main__':
    main()
