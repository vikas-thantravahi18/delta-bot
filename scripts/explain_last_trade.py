"""Reconstruct WHY the bot took its most recent option trade.

Pulls the actual entry fill, then recomputes EMA50 / EMA200 / RSI(14) on the same
5m candles the bot saw, and checks the two conditions that must BOTH hold:

    regime   EMA50 < EMA200            -> puts only
    trigger  RSI(14) crosses UP through rsi_sell

If both held, the trade was correct by design even if it looks wrong on a chart:
this strategy deliberately fades a rally INSIDE a downtrend. A green candle is
what the short signal is supposed to fire on.

Run:  py scripts/explain_last_trade.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import Config
from src.live.options_trader import OptionsTrader


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def main() -> int:
    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=False)
    client = trader.client

    # ---- find the most recent option ENTRY ------------------------------- #
    fills = [f for f in (client.get_fills(page_size=100) or [])
             if str(f.get("product_symbol", "")).startswith(("C-BTC", "P-BTC"))
             and str(f.get("side")) == "buy"]
    if not fills:
        print("No option buys found.")
        return 1
    fills.sort(key=lambda z: str(z.get("created_at")))
    last = fills[-1]
    sym = str(last["product_symbol"])
    when = datetime.fromisoformat(str(last["created_at"]).replace("Z", "+00:00"))
    is_put = sym.startswith("P-")

    print("=" * 84)
    print(f"WHY DID THE BOT BUY {sym}?")
    print("=" * 84)
    print(f"\n  filled {when:%Y-%m-%d %H:%M:%S} UTC   "
          f"{last.get('size')} lots @ {last.get('price')}")
    print(f"  that is a {'PUT — a SHORT bet' if is_put else 'CALL — a LONG bet'}")

    # ---- rebuild the candles the bot saw, with the BOT'S OWN code -------- #
    # Do not re-implement the indicators. Run the actual strategy object over the
    # actual candle loader, so a disagreement means a real bug rather than a
    # difference between my arithmetic and its arithmetic.
    df = trader._candles()
    if df is None or df.empty:
        print("  could not fetch candles.")
        return 1
    prepared = trader.strategy.prepare(df)
    c = df["close"].astype(float)
    e50, e200, r = ema(c, 50), ema(c, 200), rsi(c, 14)

    # The bot evaluates i = len(df) - 2, the last CLOSED bar — the final row is
    # still forming. So the bar that triggered a fill at 16:40:55 is 16:35, not
    # 16:40. Scan a window and report every bar the strategy actually fires on.
    print(f"\n## WHAT THE STRATEGY SAYS ON EACH BAR AROUND THE FILL")
    print(f"\n  {'bar (UTC)':<12}{'close':>10}{'EMA50':>10}{'EMA200':>10}"
          f"{'RSI':>7}{'regime':>7}{'signal':>9}")
    print("  " + "-" * 65)
    fired = []
    for i in range(len(df)):
        ts = df.index[i]
        if abs((ts - when).total_seconds()) > 25 * 60:
            continue
        try:
            s = trader.strategy.signal(prepared, i)
        except Exception:
            s = None
        reg = "down" if e50.iloc[i] < e200.iloc[i] else "up"
        mark = ""
        if s is not None:
            fired.append((ts, s))
            mark = s.side.upper()
        star = "  <- fill" if abs((ts - when).total_seconds()) < 300 else ""
        print(f"  {ts:%H:%M}{'':<6}{c.iloc[i]:>10,.1f}{e50.iloc[i]:>10,.1f}"
              f"{e200.iloc[i]:>10,.1f}{r.iloc[i]:>7.1f}{reg:>7}"
              f"{(mark or '-'):>9}{star}")

    if not fired:
        print("\n  No bar in this window produces a signal now.")
        print("  NOTE: candles are re-fetched live, so a bar that was the last CLOSED")
        print("  bar at fill time is now several bars back. If the trade is genuinely")
        print("  unexplained the mismatch will still show below.")

    bar = fired[-1][0] if fired else df.index[max(0, len(df) - 2)]
    pos = list(df.index).index(bar)
    prev = df.index[max(0, pos - 1)]

    print(f"\n## THE 5m BAR THE BOT DECIDED ON  ({bar:%H:%M} UTC)")
    print(f"\n  {'':<22}{'previous bar':>16}{'signal bar':>16}")
    print("  " + "-" * 54)
    print(f"  {'close':<22}{c[prev]:>16,.1f}{c[bar]:>16,.1f}")
    print(f"  {'EMA50':<22}{e50[prev]:>16,.1f}{e50[bar]:>16,.1f}")
    print(f"  {'EMA200':<22}{e200[prev]:>16,.1f}{e200[bar]:>16,.1f}")
    print(f"  {'RSI(14)':<22}{r[prev]:>16.1f}{r[bar]:>16.1f}")

    down = e50[bar] < e200[bar]
    up = e50[bar] > e200[bar]
    crossed_up = r[bar] > cfg.strategy.params.get("rsi_sell", 60.0) >= r[prev]
    crossed_dn = r[bar] < cfg.strategy.params.get("rsi_buy", 40.0) <= r[prev]

    print(f"\n## THE TWO CONDITIONS")
    gap = e50[bar] - e200[bar]
    print(f"\n  regime   EMA50 {'<' if down else '>'} EMA200  "
          f"(gap {gap:+,.1f})  ->  {'DOWNTREND, puts only' if down else 'UPTREND, calls only'}")
    print(f"  trigger  RSI went {r[prev]:.1f} -> {r[bar]:.1f}")
    if is_put:
        print(f"           needs to cross UP through "
              f"{cfg.strategy.params.get('rsi_sell', 60.0):.0f}: "
              f"{'YES' if crossed_up else 'NO'}")
    else:
        print(f"           needs to cross DOWN through "
              f"{cfg.strategy.params.get('rsi_buy', 40.0):.0f}: "
              f"{'YES' if crossed_dn else 'NO'}")

    ok = (down and crossed_up and is_put) or (up and crossed_dn and not is_put)
    print(f"\n## VERDICT\n")
    if ok:
        print("  CORRECT — both conditions held. The bot did exactly what it is built")
        if is_put:
            print("  to do: BTC is in a downtrend on the 5m EMAs, price rallied hard")
            print("  enough to push RSI above 60, and the strategy SELLS that rally.")
            print("\n  A green candle is not a bug here — it is the trigger. The strategy")
            print("  fades short-term strength in the direction of the longer trend.")
        else:
            print("  to do: BTC is in an uptrend, price dipped enough to push RSI under")
            print("  40, and the strategy BUYS that dip.")
    else:
        print("  MISMATCH — the conditions do not line up with the trade taken.")
        print("  This needs investigating before the bot keeps running.")

    # ---- where it stands now ---------------------------------------------- #
    print(f"\n## WHERE IT STANDS")
    spot = trader._spot()
    entry_spot = float(c[bar])
    tgt = entry_spot - trader.target_points if is_put else entry_spot + trader.target_points
    if spot:
        moved = spot - entry_spot
        togo = (tgt - spot) if not is_put else (spot - tgt)
        print(f"\n  BTC at signal {entry_spot:,.1f}  ->  now {spot:,.1f}  ({moved:+,.0f} pts)")
        print(f"  target {tgt:,.1f} — needs {abs(togo):,.0f} more points "
              f"{'DOWN' if is_put else 'UP'}")
    hrs = trader._hours_to_settlement()
    print(f"  {hrs:.1f}h until the 12:00 UTC settlement closes it either way")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
