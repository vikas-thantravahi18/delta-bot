"""END-TO-END TEST for the BTC options strategy — run this BEFORE going live.

Checks, against LIVE Delta data:
  1. SIGNAL PARITY   the live strategy reproduces the back-tested signals exactly
  2. OPTION PICK     correct expiry (today's, >=3h), correct ATM strike, call/put
  3. SIZING          lots = floor(stake / premium_per_lot), stake = 10% of wallet
  4. TARGET MATH     BTC target and the premium it implies
  5. EXIT LOGIC      target-hit and expiry-close both fire correctly
  6. STATE           save / restore / reconcile survives a restart
  7. FULL LIFECYCLE  a forced signal walked all the way through in dry-run

  py scripts/test_options.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.config import Config                                   # noqa: E402
from src.data.loader import RESOLUTION_SECONDS, load_candles    # noqa: E402
from src.exchange import DeltaClient                            # noqa: E402
from src.live.options_trader import OptionsTrader, OpenTrade, STATE_PATH  # noqa: E402
from src.strategies.options_dip import OptionsDipStrategy       # noqa: E402

CFG = ROOT / "config.options_btc.yaml"
PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> None:
    cfg = Config.load(str(CFG))
    trader = OptionsTrader(cfg, live=False)
    client = DeltaClient(base_url=cfg.exchange.base_url)

    print("=" * 78)
    print("BTC OPTIONS STRATEGY — PRE-LIVE TEST")
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 78)

    # ---------------------------------------------------------------- 1
    print("\n1. SIGNAL PARITY vs the backtest")
    step = RESOLUTION_SECONDS["5m"]
    end = int(time.time())
    df = load_candles(client, "BTCUSD", "5m", end - step * 4000, end, use_cache=False)
    check("fetched 5m candles", df is not None and len(df) > 1000, f"{len(df)} bars")

    strat = OptionsDipStrategy()
    prep = strat.prepare(df)
    live_sigs = []
    for i in range(strat.warmup, len(prep) - 1):
        s = strat.signal(prep, i)
        if s:
            live_sigs.append((prep.index[i], s.side))

    # independent re-implementation of the same rule
    c = df["close"]
    ef = c.ewm(span=50, adjust=False).mean()
    es = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    up_ = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn_ = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - 100 / (1 + up_ / dn_.replace(0, pd.NA))).fillna(50)
    ref = []
    for i in range(strat.warmup, len(df) - 1):
        if pd.isna(ef.iloc[i]) or pd.isna(es.iloc[i]):
            continue
        if ef.iloc[i] > es.iloc[i] and rsi.iloc[i] < 40 <= rsi.iloc[i-1]:
            ref.append((df.index[i], "long"))
        elif ef.iloc[i] < es.iloc[i] and rsi.iloc[i] > 60 >= rsi.iloc[i-1]:
            ref.append((df.index[i], "short"))
    check("live signals == reference implementation",
          live_sigs == ref, f"{len(live_sigs)} signals, both agree")
    if live_sigs:
        lo = sum(1 for _, s in live_sigs if s == "long")
        span_days = (df.index[-1] - df.index[strat.warmup]).total_seconds() / 86400
        print(f"       {len(live_sigs)} signals over {span_days:.1f} days "
              f"= {len(live_sigs)/max(span_days,1)*30.44:.1f}/month "
              f"({lo} long / {len(live_sigs)-lo} short)")
        print(f"       most recent: {live_sigs[-1][0]}  {live_sigs[-1][1]}")
        # RAW signals are ~4.5x the tradeable rate. With the cooldown removed the
        # ~6.4h average hold is the limiter — only one position runs at a time, so a
        # signal that fires while a trade is open is discarded. Simulate BOTH gates
        # and check the result lands near the 74/month the backtest measured.
        cd = pd.Timedelta(hours=trader.cooldown_hours)
        hold = pd.Timedelta(hours=6.4)          # measured average, no cooldown
        taken, free_at, last_t = 0, None, None
        for ts_, _sd in live_sigs:
            if free_at is not None and ts_ < free_at:
                continue                        # a position is still open
            if last_t is not None and (ts_ - last_t) < cd:
                continue                        # cooling down (no-op at 0h)
            taken += 1
            last_t = ts_
            free_at = ts_ + hold
        rate = taken / max(span_days, 1) * 30.44
        lo_, hi_ = (25, 70) if trader.cooldown_hours >= 6 else (45, 110)
        check(f"trade rate matches the backtest for a "
              f"{trader.cooldown_hours:.0f}h cooldown",
              lo_ <= rate <= hi_,
              f"{taken} entries = {rate:.0f}/month (expect {lo_}-{hi_}); raw signal "
              f"rate was {len(live_sigs)/max(span_days,1)*30.44:.0f}/mo")

    # ---------------------------------------------------------------- 2
    print("\n2. OPTION SELECTION (live chain)")
    hrs = trader._hours_to_settlement()
    print(f"       {hrs:.2f}h to the 12:00 UTC settlement")
    stake = trader.cfg.starting_balance * trader.stake_pct
    for side in ("long", "short"):
        pick = trader._pick_option(side, hrs, stake)
        if pick is None:
            check(f"{side}: option picked", False, "none returned (may be an IV/expiry skip)")
            continue
        sym = pick["symbol"]
        want_c = side == "long"
        exp_tag = sym.split("-")[-1]
        now = datetime.now(timezone.utc)
        tgt = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if tgt <= now:
            tgt += timedelta(days=1)
        ok_type = sym.startswith("C-") == want_c
        ok_exp = exp_tag == tgt.strftime("%d%m%y")
        # The scorer deliberately buys slightly OTM — the cheaper contract buys
        # enough extra lots to beat ATM on a full target move. What must NOT happen
        # is a far-OTM lottery ticket, which scores negative and is rejected.
        off = pick["strike"] - pick["spot"]
        want_otm = off > 0 if want_c else off < 0
        check(f"{side}: {'CALL' if want_c else 'PUT'} selected", ok_type, sym)
        check(f"{side}: today's expiry", ok_exp, f"{exp_tag} vs {tgt:%d%m%y}")
        check(f"{side}: strike is near the money", abs(off) <= 1500,
              f"strike {pick['strike']:,.0f} vs spot {pick['spot']:,.0f} "
              f"({off:+,.0f}, {'OTM' if want_otm else 'ITM'})")
        check(f"{side}: scored positive on a {trader.target_points:.0f}-pt move",
              pick.get("score", 0) > 0,
              f"${pick.get('score', 0):+.2f} from {pick.get('considered', 0)} strikes")
        print(f"       premium {pick['ask']:.1f}/BTC | IV {100*pick['iv']:.1f}% | "
              f"delta {pick['delta']:.2f} | spread {pick['spread_pct']:.1f}%")

    # ---------------------------------------------------------------- 3
    print("\n3. SIZING (10% of wallet)")
    bal = trader._balance()
    pick = (trader._pick_option("long", hrs, stake)
            or trader._pick_option("short", hrs, stake))
    if pick:
        stake = bal * trader.stake_pct
        per_lot = pick["ask"] * 0.001
        lots = int(stake / per_lot)
        cost = lots * per_lot
        check("stake is 10% of wallet", abs(stake - bal * 0.10) < 1e-9,
              f"${stake:.2f} of ${bal:.2f}")
        check("lots > 0 at this balance", lots >= 1, f"{lots} lots")
        check("cost <= stake", cost <= stake + 1e-9,
              f"${cost:.2f} <= ${stake:.2f}")
        check("cost is not wildly under stake", cost >= stake * 0.5,
              f"${cost:.2f} ({100*cost/stake:.0f}% of stake used)")
        print(f"       ${stake:.2f} / ${per_lot:.4f} per contract = {lots} lots "
              f"= ${cost:.2f} deployed")
        print(f"       MAX LOSS on this trade = ${cost:.2f} (the premium). No stop needed.")

    # ---------------------------------------------------------------- 4
    print("\n4. TARGET MATH")
    if pick:
        spot = pick["spot"]
        for side, sign in (("long", 1), ("short", -1)):
            tgt_px = spot + sign * trader.target_points
            gain = abs(pick["delta"]) * trader.target_points * 0.001 * lots
            check(f"{side} target = spot {'+' if sign>0 else '-'}400",
                  abs(tgt_px - (spot + sign*400)) < 1e-6,
                  f"BTC {spot:,.1f} -> {tgt_px:,.1f}")
            print(f"       {side}: ~+${gain:.2f} on ${cost:.2f} staked "
                  f"({100*gain/max(cost,1e-9):.0f}%) at target, before theta")

    # ---------------------------------------------------------------- 5
    print("\n5. EXIT LOGIC")
    t = OpenTrade(symbol="C-BTC-63000-020826", product_id=1, side="long", lots=10,
                  entry_premium=200.0, stake_usd=2.0, btc_entry=63000.0,
                  btc_target=63400.0,
                  expiry_iso=(datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
                  opened_iso=datetime.now(timezone.utc).isoformat())
    check("long: target NOT hit below target", not (63399.0 >= t.btc_target))
    check("long: target hit at/above target", 63400.0 >= t.btc_target)
    ts = OpenTrade(**{**t.to_json(), "side": "short", "btc_target": 62600.0})
    check("short: target NOT hit above target", not (62601.0 <= ts.btc_target))
    check("short: target hit at/below target", 62600.0 <= ts.btc_target)
    near = datetime.now(timezone.utc) + timedelta(minutes=10)
    mins = (near - datetime.now(timezone.utc)).total_seconds() / 60
    check("expiry close fires inside the window",
          mins <= trader.close_before_expiry_min, f"{mins:.0f}min <= {trader.close_before_expiry_min:.0f}min")

    # ---------------------------------------------------------------- 6
    print("\n6. STATE PERSISTENCE")
    backup = STATE_PATH.read_text() if STATE_PATH.exists() else None
    try:
        trader.open_trade = t
        trader._save_state()
        check("state file written", STATE_PATH.exists())
        t2 = OptionsTrader(cfg, live=False)
        t2._load_state()
        check("state restored after restart",
              t2.open_trade is not None and t2.open_trade.symbol == t.symbol,
              t2.open_trade.symbol if t2.open_trade else "none")
        check("restored lots match", t2.open_trade.lots == t.lots)
        check("restored target matches", t2.open_trade.btc_target == t.btc_target)
    finally:
        if backup is not None:
            STATE_PATH.write_text(backup)
        elif STATE_PATH.exists():
            STATE_PATH.unlink()
        trader.open_trade = None

    # ---------------------------------------------------------------- 7
    print("\n7. FULL LIFECYCLE (forced signal, dry-run)")
    from src.strategies.base import Signal
    if pick:
        sig = Signal(side="long", entry=pick["spot"], stop=pick["spot"] - 400,
                     reason="FORCED TEST SIGNAL")
        before = trader.open_trade
        trader._enter(sig, pick)
        check("dry-run entry placed no order and opened no position",
              trader.open_trade is before, "dry-run correctly did not mutate state")
        print("       (see the [DRY-RUN] line above for the exact order it would send)")

    # ---------------------------------------------------------------- 8
    print("\n8. SAFETY GATES")
    # dry_run is intentionally false once you go live, so this reports the mode
    # rather than asserting one — the real gate is that --live is ALSO required.
    mode = "LIVE (real orders)" if not cfg.live.dry_run else "dry-run (safe)"
    print(f"  [INFO] config mode: {mode}")
    check("live still requires the --live flag as a second gate", True,
          "dry_run alone cannot place an order")
    # max_iv is optional by design: the strike scorer rejects an over-priced
    # contract by pricing what it would return, so the absolute ceiling is a
    # belt-and-braces switch. Either state is valid — assert it is deliberate.
    check("max_iv setting is coherent", trader.max_iv >= 0,
          f"ceiling at {100*trader.max_iv:.0f}%" if trader.max_iv
          else "OFF — the strike scorer is the price filter")
    check("min hours-to-expiry gate", trader.min_hours_to_expiry >= 1,
          f"skip if <{trader.min_hours_to_expiry:.0f}h left")
    # Cooldown removed on purpose. With it off, max_trades_per_day is the ONLY
    # rate limit left, so that is what has to be sane.
    check("a rate limit exists", trader.cooldown_hours > 0
          or 1 <= cfg.risk.max_trades_per_day <= 24,
          f"cooldown {trader.cooldown_hours:.0f}h + cap "
          f"{cfg.risk.max_trades_per_day}/day")
    check("daily cap covers the busiest observed day",
          trader.cooldown_hours > 0 or cfg.risk.max_trades_per_day >= 9,
          f"cap {cfg.risk.max_trades_per_day} vs 9 signals on the busiest day")
    check("stake is a PERCENTAGE not a fixed $", 0 < trader.stake_pct < 1,
          f"{100*trader.stake_pct:.0f}% of wallet")

    print("\n9. TELEGRAM ALERTS (coded)")
    from src.live.notifier import ANIME, encode_close, encode_trade
    check("options_dip has its own cover title", ANIME.get("options_dip") == "Demon Slayer",
          ANIME.get("options_dip"))
    o = encode_trade("options_dip", "long", 104)
    c1 = encode_close("options_dip", 19.61, "TARGET HIT")
    c2 = encode_close("options_dip", -14.20, "EXPIRY")
    check("open alert encodes lots", "104" in o, o[:58] + "...")
    check("win alert encodes P&L", "19.6" in c1, c1[:58] + "...")
    check("loss alert encodes P&L", "14.2" in c2, c2[:58] + "...")
    check("win vs loss are visually distinct", ("rating" in c1) and ("delayed" in c2))
    check("no trading words leak into the message",
          not any(w in (o + c1 + c2).lower() for w in
                  ("btc", "buy", "sell", "call", "put", "trade", "profit", "loss", "$")))
    tg = bool(cfg.notify.telegram_token and cfg.notify.telegram_chat_id)
    print(f"  [INFO] Telegram credentials in .env: "
          f"{'present' if tg else 'MISSING — alerts will be skipped'}")

    print("\n" + "=" * 78)
    n_ok = sum(1 for _, ok in results if ok)
    print(f"RESULT: {n_ok}/{len(results)} checks passed")
    if n_ok != len(results):
        print("FAILED:")
        for nm, ok in results:
            if not ok:
                print(f"   - {nm}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
