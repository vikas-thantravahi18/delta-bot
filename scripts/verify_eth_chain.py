"""LIVE ETH CHAIN VERIFICATION — the last gate before any code change.

Everything the ETH backtest assumed but never checked against the exchange:

  1. settlement time      assumed 12:00 UTC, same as BTC
  2. contract_value       assumed 0.01 ETH per lot
  3. minimum order size   never checked
  4. order-book depth     the model wants ~212 lots; is that there?
  5. fee schedule         assumed identical to BTC's
  6. can the BOT pick an ETH option at all — underlying is hardcoded to BTC

Read-only. Places no orders. Run:  py scripts/verify_eth_chain.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "https://api.india.delta.exchange/v2"
WANT_LOTS = 212          # what the $15-stake model buys at the tuned config
OK, BAD, WARN = [], [], []


def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def check(name, cond, detail="", warn_only=False):
    if cond:
        OK.append(name)
        tag = "PASS"
    elif warn_only:
        WARN.append(name)
        tag = "WARN"
    else:
        BAD.append(name)
        tag = "FAIL"
    print(f"  [{tag}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    now = datetime.now(timezone.utc)
    print("=" * 84)
    print(f"LIVE ETH OPTION CHAIN — {now:%Y-%m-%d %H:%M} UTC")
    print("=" * 84)

    chain = [t for t in get(f"{API}/tickers?contract_types=call_options,put_options")
             .get("result", []) if t.get("underlying_asset_symbol") == "ETH"]
    if not check("ETH options exist on Delta India", len(chain) > 0,
                 f"{len(chain)} contracts"):
        return 1
    spot = float(next(t["spot_price"] for t in chain if t.get("spot_price")))
    print(f"       ETH spot ${spot:,.2f}")

    # front expiry, ATM strike
    tags = sorted({t["symbol"].split("-")[-1] for t in chain})
    front = tags[0]
    day = [t for t in chain if t["symbol"].endswith(front)]
    atm = min(day, key=lambda z: abs(float(z["strike_price"]) - spot))
    sym = atm["symbol"]
    print(f"       front expiry {front}, ATM contract {sym}")

    # ---------------------------------------------------------------- 1
    print("\n## 1. CONTRACT SPECS")
    prod = get(f"{API}/products/{sym}").get("result") or {}
    cv = prod.get("contract_value")
    check("contract_value is 0.01 ETH",
          cv is not None and abs(float(cv) - 0.01) < 1e-9,
          f"exchange says {cv}")
    st = prod.get("settlement_time")
    check("settlement_time present", bool(st), str(st))
    if st:
        s_dt = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
        check("settles at 12:00 UTC (same as BTC)", s_dt.hour == 12,
              f"{s_dt:%Y-%m-%d %H:%M} UTC")
        print(f"       {(s_dt - now).total_seconds()/3600:.1f}h to settlement")
    tick = prod.get("tick_size")
    print(f"       tick size {tick}")
    for k in ("min_size", "minimum_size", "lot_size"):
        if prod.get(k) is not None:
            print(f"       {k}: {prod[k]}")

    # ---------------------------------------------------------------- 2
    print("\n## 2. ORDER BOOK DEPTH")
    q = atm.get("quotes") or {}
    bid, ask = float(q.get("best_bid") or 0), float(q.get("best_ask") or 0)
    bsz, asz = float(q.get("bid_size") or 0), float(q.get("ask_size") or 0)
    mark = float(atm["mark_price"])
    print(f"       {sym}: bid {bid:.2f} x {bsz:,.0f}   ask {ask:.2f} x {asz:,.0f}")
    print(f"       mark {mark:.2f}   IV {100*float(atm.get('mark_vol') or 0):.1f}%")
    check(f"ask depth covers {WANT_LOTS} lots", asz >= WANT_LOTS,
          f"{asz:,.0f} available")
    check(f"bid depth covers {WANT_LOTS} lots (to exit)", bsz >= WANT_LOTS,
          f"{bsz:,.0f} available")
    if mark > 0 and bid > 0:
        sp = 100 * (ask - bid) / mark
        check("spread under 12%", sp <= 12.0, f"{sp:.1f}%")

    # ---------------------------------------------------------------- 3
    print("\n## 3. WHAT A $15 STAKE ACTUALLY BUYS")
    if ask > 0 and cv:
        per_lot = ask * float(cv)
        lots = int(15.0 / per_lot)
        print(f"       one lot costs ${per_lot:.4f}  ->  $15 buys {lots:,} lots")
        print(f"       = {lots*float(cv):.2f} ETH of exposure "
              f"(${lots*float(cv)*spot:,.0f} notional)")
        check("stake buys a tradeable size", lots >= 1, f"{lots:,} lots")
        check("size fits the book", lots <= asz if asz else True,
              f"{lots:,} wanted vs {asz:,.0f} offered", warn_only=True)

    # ---------------------------------------------------------------- 4
    print("\n## 4. CAN THE BOT PICK AN ETH OPTION TODAY?")
    try:
        from src.config import Config
        from src.live.options_trader import OptionsTrader
        cfg = Config.load(str(ROOT / "config.ut_stc_eth_options.yaml"))
        tr = OptionsTrader(cfg, live=False)
        eth_n = len([t for t in tr.client.get_option_tickers(tr.option_asset)
                     if str(t.get("underlying_asset_symbol")) == "ETH"])
        check("client fetches the ETH chain", eth_n > 0, f"{eth_n} contracts")
        check("trader is wired to ETH", tr.underlying == "ETHUSD",
              f"underlying={tr.underlying} asset={tr.option_asset}")
        check("lot size is ETH's 0.01", abs(tr.lot_size - 0.01) < 1e-9,
              str(tr.lot_size))
        check("strategy is ut_stc", type(tr.strategy).__name__ == "UtStcStrategy",
              type(tr.strategy).__name__)
        check("running on 4h bars", cfg.market.resolution == "4h",
              cfg.market.resolution)
        pick = tr._pick_option("long", 6.0, tr._balance() * tr.stake_pct)
        check("it can pick a live ETH contract", pick is not None,
              pick["symbol"] if pick else "none")
    except Exception as exc:
        check("bot code loads", False, str(exc))

    # ---------------------------------------------------------------- 5
    print("\n## 5. FEE ESTIMATE (unverified — no ETH fill on record)")
    if ask > 0 and cv:
        lots = int(15.0 / (ask * float(cv)))
        notional = lots * float(cv) * spot
        prem = lots * ask * float(cv)
        modelled = min(0.000354 * notional, 0.0413 * prem)
        print(f"       {lots:,} lots: notional ${notional:,.0f}, premium ${prem:.2f}")
        print(f"       model charges ${modelled:.4f} per side "
              f"({100*modelled/prem:.1f}% of premium)")
        print(f"       BTC measured 2.4% of premium on a real fill — ETH unverified")

    # ---------------------------------------------------------------- 6
    print("\n## 6. TERM STRUCTURE (backtest assumed 33.9% front, rising)")
    print(f"\n       {'expiry':<10}{'hrs':>8}{'ATM IV':>10}{'spread%':>10}")
    print("       " + "-" * 38)
    for tg in tags[:5]:
        grp = [t for t in chain if t["symbol"].endswith(tg)]
        a = min(grp, key=lambda z: abs(float(z["strike_price"]) - spot))
        try:
            exp = datetime(2000 + int(tg[4:6]), int(tg[2:4]), int(tg[0:2]),
                           12, tzinfo=timezone.utc)
            hrs = (exp - now).total_seconds() / 3600
        except Exception:
            continue
        qq = a.get("quotes") or {}
        b2, a2 = float(qq.get("best_bid") or 0), float(qq.get("best_ask") or 0)
        mk = float(a["mark_price"])
        sp = 100 * (a2 - b2) / mk if (b2 > 0 and mk > 0) else float("nan")
        print(f"       {tg:<10}{hrs:>8.1f}"
              f"{100*float(a.get('mark_vol') or 0):>9.1f}%{sp:>9.1f}%")

    print("\n" + "=" * 84)
    print(f"{len(OK)} passed, {len(WARN)} warnings, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    for w in WARN:
        print(f"  WARN:   {w}")
    print("=" * 84)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
