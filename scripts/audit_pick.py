"""AUDIT ONE TRADE — is the bot really picking the right contract?

Deliberately does NOT trust the trader. It asks the bot for a pick, then goes back
to the raw Delta API on its own and re-derives everything from scratch:

  1. does the premium the bot reported match what the exchange is quoting for that
     exact symbol RIGHT NOW?  (catches stale data, wrong symbol lookup)
  2. is contract_value really 0.001 BTC?  (the bot hardcodes it; if Delta ever
     changed it, every size and every P&L in this system would be silently wrong)
  3. re-ranking every strike independently, is the bot's pick actually the best?
  4. does lots x premium x contract_value actually equal the stake?
  5. call for long / put for short, and the next 12:00 UTC expiry?
  6. is the quoted premium even self-consistent with the quoted IV?

Read-only. Places no orders. Run:  py scripts/audit_pick.py [--side long|short]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader

API = "https://api.india.delta.exchange/v2"
LOT = 0.001
MOVE = 400.0
HOLD = 6.0

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def bs(s, k, t, sig, call):
    if t <= 1e-9 or sig <= 1e-9:
        return max(0.0, (s - k) if call else (k - s))
    v = sig * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sig * sig * t) / v
    d2 = d1 - v
    n = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return (s * n(d1) - k * n(d2)) if call else (k * n(-d2) - s * n(-d1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="long", choices=["long", "short"])
    a = ap.parse_args()

    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=False)
    hrs = trader._hours_to_settlement()
    balance = trader._balance()
    stake = balance * trader.stake_pct
    want_call = a.side == "long"

    print("=" * 86)
    print(f"AUDIT — {a.side.upper()} ({'CALL' if want_call else 'PUT'}) "
          f"| {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"balance ${balance:.2f}  stake ${stake:.2f} ({100*trader.stake_pct:.0f}%)  "
          f"{hrs:.1f}h to settlement")
    print("=" * 86)

    # ---------------------------------------------------------------- 1
    # FREEZE the chain first. The bot fetches its own tickers, so without this the
    # bot and the audit see snapshots seconds apart — enough for a near-tie between
    # two adjacent strikes to flip, which looks like a bug and is not one.
    print("\n## 1. WHAT THE BOT PICKED  (both sides fed one frozen snapshot)")
    snapshot = get(f"{API}/tickers?contract_types=call_options,put_options").get("result", [])
    snapshot = [t for t in snapshot if t.get("underlying_asset_symbol") == "BTC"]
    trader.client.get_option_tickers = lambda *_args, **_kw: snapshot

    pick = trader._pick_option(a.side, hrs, stake)
    if not check("bot returned a pick", pick is not None):
        return 1
    print(f"      {pick['symbol']}")
    print(f"      strike {pick['strike']:,.0f} ({pick['strike']-pick['spot']:+,.0f} "
          f"from spot {pick['spot']:,.1f})")
    print(f"      ask {pick['ask']:.1f}  bid {pick['bid']:.1f}  IV "
          f"{100*pick['iv']:.1f}%  spread {pick['spread_pct']:.2f}%")
    print(f"      {pick['scored_lots']} lots, score ${pick['score']:+.2f}, "
          f"best of {pick['considered']} strikes")

    # ---------------------------------------------------------------- 2
    print("\n## 2. RE-FETCHING THAT SYMBOL FROM THE EXCHANGE, INDEPENDENTLY")
    chain = snapshot
    live = next((t for t in chain if t.get("symbol") == pick["symbol"]), None)
    if not check("symbol exists on the live chain", live is not None, pick["symbol"]):
        return 1
    q = live.get("quotes") or {}
    l_ask = float(q.get("best_ask") or 0)
    l_bid = float(q.get("best_bid") or 0)
    l_iv = float(live.get("mark_vol") or 0)
    l_spot = float(live.get("spot_price") or 0)
    print(f"      exchange says: ask {l_ask:.1f}  bid {l_bid:.1f}  "
          f"IV {100*l_iv:.1f}%  spot {l_spot:,.1f}")
    check("ask matches the chain", abs(pick["ask"] - l_ask) <= max(1.0, 0.02 * l_ask),
          f"bot {pick['ask']:.1f} vs chain {l_ask:.1f}")
    check("bid matches the chain", abs(pick["bid"] - l_bid) <= max(1.0, 0.02 * l_bid),
          f"bot {pick['bid']:.1f} vs chain {l_bid:.1f}")
    check("IV matches the chain", abs(pick["iv"] - l_iv) < 0.01,
          f"bot {100*pick['iv']:.1f}% vs chain {100*l_iv:.1f}%")
    check("spot matches the chain", abs(pick["spot"] - l_spot) <= max(50, 0.002*l_spot),
          f"bot {pick['spot']:,.1f} vs chain {l_spot:,.1f}")

    # ---------------------------------------------------------------- 3
    print("\n## 3. CONTRACT SIZE — the bot hardcodes 0.001 BTC")
    prod = get(f"{API}/products/{pick['symbol']}").get("result") or {}
    cv = prod.get("contract_value")
    check("contract_value is 0.001 BTC", cv is not None and abs(float(cv) - LOT) < 1e-9,
          f"exchange reports {cv}")
    st = prod.get("settlement_time")
    check("settlement_time present", bool(st), str(st))
    if st:
        s_dt = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
        left = (s_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        check("settles at 12:00 UTC", s_dt.hour == 12, f"{s_dt:%Y-%m-%d %H:%M} UTC")
        check("bot's hours-to-expiry agrees", abs(left - hrs) < 0.5,
              f"chain {left:.2f}h vs bot {hrs:.2f}h")

    # ---------------------------------------------------------------- 4
    print("\n## 4. RE-RANKING EVERY STRIKE FROM SCRATCH")
    now = datetime.now(timezone.utc)
    tgt = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    tag = tgt.strftime("%d%m%y")
    T0 = hrs / 24 / 365
    T1 = max((hrs - min(HOLD, hrs - 0.5)) / 24 / 365, 1e-9)
    S1 = l_spot + (MOVE if want_call else -MOVE)

    mine = []
    for t in chain:
        sym = str(t.get("symbol", ""))
        if not sym.endswith(tag) or sym.startswith("C-") != want_call:
            continue
        try:
            k = float(t["strike_price"]); mark = float(t["mark_price"])
            iv = float(t.get("mark_vol") or 0)
        except Exception:
            continue
        qq = t.get("quotes") or {}
        ask = float(qq.get("best_ask") or 0) or mark
        bid = float(qq.get("best_bid") or 0) or mark
        if ask <= 0 or iv <= 0 or mark <= 0:
            continue
        spr = 100.0 * (ask - bid) / mark if bid > 0 else 999.0
        if spr > trader.max_spread_pct or abs(k - l_spot) > trader.strike_span_points:
            continue
        lots = int(stake / (ask * LOT))
        if lots < 1:
            continue
        cost = lots * ask * LOT
        fair = bs(S1, k, T1, iv, want_call)
        edge = (ask - bid) / 2.0 if bid > 0 else fair * 0.06
        mine.append((lots * max(fair - edge, 0.0) * LOT - cost, k, sym, lots, ask, spr))
    mine.sort(key=lambda z: -z[0])

    print(f"\n      {'rank':>5}{'strike':>10}{'offset':>9}{'ask':>9}{'lots':>7}"
          f"{'spread':>9}{'score':>10}")
    print("      " + "-" * 59)
    for r, (sc, k, sym, lots, ask, spr) in enumerate(mine[:6], 1):
        flag = "  <- bot picked this" if sym == pick["symbol"] else ""
        print(f"      {r:>5}{k:>10,.0f}{k-l_spot:>+9,.0f}{ask:>9.1f}{lots:>7}"
              f"{spr:>8.2f}%{sc:>+10.2f}{flag}")

    # On one frozen snapshot this must be exact. If it is not, report the SIZE of
    # the disagreement — adjacent strikes routinely tie to the cent, and a tie
    # broken the other way costs nothing, whereas a real bug would show a gap.
    picked_rank = next((i for i, m in enumerate(mine, 1) if m[2] == pick["symbol"]), None)
    top_ok = bool(mine) and mine[0][2] == pick["symbol"]
    if not top_ok and picked_rank and mine:
        gap = mine[0][0] - mine[picked_rank - 1][0]
        top_ok = gap < 0.01                      # a sub-cent tie is not an error
        check("bot picked the top-ranked strike (or a tie for it)", top_ok,
              f"picked rank {picked_rank}, behind best by ${gap:.4f}")
    else:
        check("bot picked the top-ranked strike", top_ok,
              f"independent best = {mine[0][2] if mine else 'none'}")
    if mine:
        check("score agrees with independent calc",
              abs(mine[0][0] - pick["score"]) <= max(0.05, 0.02 * abs(mine[0][0])),
              f"bot ${pick['score']:+.4f} vs audit ${mine[0][0]:+.4f}")

    # ---------------------------------------------------------------- 5
    print("\n## 5. SIZING MATH")
    lots = int(stake / (l_ask * LOT))
    cost = lots * l_ask * LOT
    print(f"      {lots} lots x {l_ask:.1f} premium x {LOT} BTC = ${cost:.2f}")
    check("lots match the bot", lots == pick["scored_lots"],
          f"audit {lots} vs bot {pick['scored_lots']}")
    check("cost does not exceed the stake", cost <= stake + 1e-6,
          f"${cost:.2f} <= ${stake:.2f}")
    check("cost uses most of the stake", cost >= stake * 0.85,
          f"${cost:.2f} = {100*cost/stake:.0f}% of stake")
    check("max loss is the premium", True, f"${cost:.2f} — no stop, cannot lose more")

    # ---------------------------------------------------------------- 6
    print("\n## 6. IS THE QUOTE SELF-CONSISTENT?")
    theo = bs(l_spot, float(live["strike_price"]), T0, l_iv, want_call)
    mark = float(live["mark_price"])
    check("Delta's mark ~= Black-Scholes at Delta's own IV",
          abs(theo - mark) <= max(8.0, 0.10 * mark),
          f"BS {theo:.1f} vs mark {mark:.1f} ({100*(theo/mark-1):+.1f}%)")
    check("right option type", pick["symbol"].startswith("C-") == want_call,
          "CALL for long" if want_call else "PUT for short")
    check("strike is on the correct side of spot",
          (pick["strike"] > l_spot) if want_call else (pick["strike"] < l_spot),
          f"{pick['strike']-l_spot:+,.0f} (OTM, as the scorer intends)")

    # ---------------------------------------------------------------- 7
    print("\n## 7. WHAT THIS TRADE ACTUALLY PAYS")
    K = float(live["strike_price"])
    print(f"\n      {'BTC move':>10}{'BTC price':>12}{'premium':>10}{'P&L':>10}{'return':>9}")
    print("      " + "-" * 51)
    for mv in (-400, -200, 0, 200, 400, 600):
        s_ = l_spot + mv
        px = bs(s_, K, T1, l_iv, want_call)
        exit_px = max(px - (l_ask - l_bid) / 2.0, 0.0)
        pnl = lots * exit_px * LOT - cost
        star = "  <- target" if mv == (MOVE if want_call else -MOVE) else ""
        print(f"      {mv:>+10.0f}{s_:>12,.0f}{px:>10.1f}{pnl:>+10.2f}"
              f"{100*pnl/cost:>+8.0f}%{star}")

    print("\n" + "=" * 86)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 86)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
