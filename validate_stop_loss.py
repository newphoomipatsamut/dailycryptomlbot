#!/usr/bin/env python3
"""
One-off manual validation of the live order-placement code path
(crypto_daily_ml_v3.py session 9 functions) against a REAL Kraken order
with REAL capital. See PROGRESS.md "Open items" — this must pass before
PAPER_MODE is ever flipped false in the daily workflow.

RUN THIS YOURSELF, IN YOUR OWN TERMINAL, with KRAKEN_API_KEY /
KRAKEN_SECRET set in your shell env (trade-scope keys, small funded USDT
balance). Do not paste your keys into chat or run this through an
assistant tool — this places real orders with real money and you should
be watching it live with the ability to abort.

What this DOES validate (all via the actual production functions, not
reimplementations):
  - place_live_order()        real limit buy fills as taker
  - place_stop_loss_order()   real resting stop-loss-market order is
                               accepted by Kraken
  - fetch_open_orders()       confirms the stop is actually resting on
                               the book, not just that the API returned
                               an id
  - cancel_stop_before_exit() the resting stop cancels cleanly
  - place_live_sell()         real market sell flattens the position

What this does NOT validate:
  - Whether the stop actually TRIGGERS and FILLS on a real downward move.
    That can't be forced cleanly without either waiting for a real crash
    or setting the trigger unrealistically close to market (risking a
    Kraken rejection or an unintended early fill). Treat trigger-and-fill
    as still unverified even after a clean PASS here.

Safety: a `finally` block runs on every exit path (including exceptions)
and force-cancels any leftover resting stop + force-sells any leftover
position, so the script never strands real capital mid-test.
"""
import os
import sys
import time
import ccxt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crypto_daily_ml_v3 import (
    place_live_order, place_stop_loss_order,
    cancel_stop_before_exit, place_live_sell, STOP_LOSS_PCT,
)

# Cheapest per-unit of the bot's 3 symbols (ETH/SOL/LINK) — easiest to
# clear Kraken's minimum order size at a tiny notional. Keep USD_SIZE
# small: this is a mechanism test, not a strategy test.
SYMBOL   = 'LINK/USDT'
USD_SIZE = 20.0


def main():
    exchange = ccxt.kraken({
        'apiKey':          os.environ.get('KRAKEN_API_KEY', ''),
        'secret':          os.environ.get('KRAKEN_SECRET', ''),
        'enableRateLimit': True,
    })
    if not exchange.apiKey or not exchange.secret:
        print('KRAKEN_API_KEY / KRAKEN_SECRET not set in env — aborting.')
        sys.exit(1)

    exchange.load_markets()
    ticker   = exchange.fetch_ticker(SYMBOL)
    entry_px = ticker['last']
    print(f'{SYMBOL} last price: {entry_px}')
    print(f'Buying ~${USD_SIZE} worth — confirm this is intentional real money.\n')

    stop_order_id = None
    fill_qty      = 0.0

    try:
        # 1. Real buy via the actual production function
        fill = place_live_order(exchange, SYMBOL, USD_SIZE, entry_px)
        if not fill:
            print('Buy did not fill within timeout — nothing to clean up. Exiting.')
            return
        fill_qty = fill['fill_qty']
        entry_px = fill['fill_price']
        print(f'Bought {fill_qty} {SYMBOL} @ {entry_px}')

        # 2. Real resting stop-loss via the actual production function
        sl_px = entry_px * (1 - STOP_LOSS_PCT)
        stop_order_id = place_stop_loss_order(exchange, SYMBOL, fill_qty, sl_px)
        if not stop_order_id:
            raise RuntimeError('Stop-loss placement failed')
        print(f'Stop-loss resting: {stop_order_id} trigger@{sl_px}')

        # 3. Confirm it is actually resting on the book, not just an id
        time.sleep(2)
        open_orders = exchange.fetch_open_orders(SYMBOL)
        matching    = [o for o in open_orders if o['id'] == stop_order_id]
        if not matching:
            raise RuntimeError(
                f'Stop order {stop_order_id} not found in fetch_open_orders — '
                f'cannot confirm it is resting')
        print(f'Confirmed resting on book: {matching[0]}')

        # 4. Cancel it via the actual production function
        cancel_result = cancel_stop_before_exit(exchange, stop_order_id, SYMBOL)
        print(f'Cancel result: {cancel_result}')
        if cancel_result['already_filled']:
            print('Stop already filled between steps 3 and 4 — position is '
                  'flat, nothing more to do.')
            fill_qty      = 0.0
            stop_order_id = None
            return
        if not cancel_result['cancelled']:
            raise RuntimeError('Cancel unresolved — see finally block for forced flatten')
        stop_order_id = None

        # 5. Real market sell via the actual production function
        sold = place_live_sell(exchange, SYMBOL, fill_qty)
        if not sold:
            raise RuntimeError('Sell failed after stop cancel')
        print(f'Sold {sold["fill_qty"]} {SYMBOL} @ {sold["fill_price"]}')
        fill_qty = 0.0

        print('\nPASS: buy -> resting stop -> confirmed on book -> cancel -> '
              'sell, all via the real production functions.')
        print('NOTE: this does NOT confirm the stop actually triggers on a '
              'real move — only that Kraken accepts/rests/cancels it.')

    finally:
        # Guaranteed teardown: never leave a resting stop or an open
        # position behind, on any exit path.
        if stop_order_id:
            print(f'Cleanup: cancelling leftover stop {stop_order_id}')
            try:
                exchange.cancel_order(stop_order_id, SYMBOL)
            except Exception as e:
                print(f'Cleanup cancel FAILED ({e}) — check manually: {stop_order_id}')
        if fill_qty > 0:
            print(f'Cleanup: flattening leftover {fill_qty} {SYMBOL}')
            try:
                exchange.create_market_sell_order(SYMBOL, fill_qty)
            except Exception as e:
                print(f'Cleanup flatten FAILED ({e}) — MANUAL INTERVENTION '
                      f'REQUIRED for {fill_qty} {SYMBOL}')

        try:
            open_orders = exchange.fetch_open_orders(SYMBOL)
            balance     = exchange.fetch_balance()
            base        = SYMBOL.split('/')[0]
            print(f'\nFinal check — open orders for {SYMBOL}: '
                  f'{len(open_orders)} (should be 0)')
            print(f'Final check — {base} balance: '
                  f'{balance.get(base, {}).get("free", "?")} (should be ~0)')
        except Exception as e:
            print(f'Final check failed to run ({e}) — verify manually on '
                  f'the Kraken web UI.')


if __name__ == '__main__':
    main()
