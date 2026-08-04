"""Live monitoring dashboard.

  pip install streamlit
  streamlit run dashboard.py

Shows the ACTIVE strategy (BTC options — options_dip) plus the disabled perp legs,
the account balance, TOTAL RETURN %, and a trade history with REAL P&L.

Why P&L has to be computed here
-------------------------------
Delta's /v2/fills endpoint returns `realized_pnl: null` on every fill — the API
simply does not provide per-trade P&L, which is why the old P&L column was always
blank. This dashboard pairs fills FIFO per symbol and computes

    pnl = (exit - entry) x size x contract_value x direction  -  commissions

Verified against a real round trip: P-BTC-63400 bought 150 @451, sold @905.9
-> +$66.01 net, which matches Delta's own "ROE 100.86%" on that position.

SCOPE — this strategy only
--------------------------
Everything on this page counts ONLY the active strategy: option fills (C-BTC /
P-BTC) dated on or after `strategy_start` in data/dashboard_baseline.json. Older
perp trades and any manual option trades from before that timestamp are excluded,
so the win rate, P&L and total return describe options_dip and nothing else.

    py scripts/reset_dashboard.py     # re-baseline to now + current balance

Total return % = realised P&L since the start / capital at the start.

Needs DELTA_API_KEY / DELTA_API_SECRET in .env. Read-only — never places orders.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd
import streamlit as st

from src.config import Config
from src.exchange import DeltaClient

ROOT = Path(__file__).resolve().parent
BASELINE_PATH = ROOT / "data" / "dashboard_baseline.json"
BTC_CONTRACT = 0.001          # Delta BTC perp AND BTC option contract size
ETH_CONTRACT = 0.01

# Both option legs are tracked. BTC and ETH have different contract sizes,
# different strategies and different targets, so they get separate scorecards —
# a blended number would hide which one is actually working.
# Options legs get per-asset scorecards below; the perp legs share the BTC slot
# via market_lock so they are shown as one card.
ACTIVE = [
    {"name": "ut_stc", "asset": "ETH",
     "market": "ETH options · 4h · +30 pts · 10% stake",
     "tok": "Ξ", "color": "#6f7ce8",
     "match": lambda s: s.startswith(("C-ETH", "P-ETH"))},
    {"name": "v2 + ema_rsi", "asset": "BTC-perp",
     "market": "BTCUSD perp · 1h + 30m · margin 50% @ 10x",
     "tok": "₿", "color": "#f7931a",
     "match": lambda s: s == "BTCUSD"},
]
DISABLED = [
    {"name": "options_dip", "market": "BTC options · 5m — disabled 04 Aug",
     "tok": "◆", "color": "#3d4759",
     "match": lambda s: s.startswith(("C-BTC", "P-BTC"))},
    {"name": "ut_stc (perp)", "market": "ETHUSD perp · 4h", "tok": "Ξ",
     "color": "#3d4759", "match": lambda s: s == "ETHUSD"},
]


def asset_of(sym: str) -> str:
    """Which option book a fill belongs to. '' for perps and anything else."""
    s = str(sym or "")
    if s.startswith(("C-BTC", "P-BTC")):
        return "BTC"
    if s.startswith(("C-ETH", "P-ETH")):
        return "ETH"
    return ""

st.set_page_config(page_title="Delta Bot Monitor", page_icon="📊", layout="wide")

st.markdown("""
<style>
  .stApp { background:#0c121d; }
  .block-container { padding-top:1.4rem; max-width:1180px; }
  h1,h2,h3,h4,p,span,div,td,th { color:#e7ecf4; }
  .mono { font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-variant-numeric:tabular-nums; }
  .card { background:#141c2a; border:1px solid #27313f; border-radius:14px; padding:16px 18px; margin-bottom:6px; position:relative; overflow:hidden; }
  .card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--sc); }
  .card.off { opacity:.5; }
  .kpi { font-size:30px; font-weight:600; letter-spacing:-.02em; }
  .kpi.sm { font-size:24px; }
  .lbl { font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#64708a; }
  .up { color:#33cd7c; } .down { color:#ec5b62; } .dim { color:#97a3b6; }
  .pill { display:inline-block; font-size:11.5px; font-weight:650; padding:3px 10px; border-radius:100px; border:1px solid #27313f; color:#97a3b6; }
  .pill.long { color:#33cd7c; border-color:#2e6b4e; } .pill.short { color:#ec5b62; border-color:#7a3238; }
  .pill.flat { color:#97a3b6; } .pill.off { color:#6b7688; border-color:#333c4b; }
  .tok { display:inline-grid; place-items:center; width:24px; height:24px; border-radius:50%; color:#fff; font-weight:700; font-size:12px; }
  .statbox { background:#141c2a; border:1px solid #27313f; border-radius:12px; padding:11px 12px; text-align:center; }
  [data-testid="stDataFrame"] { background:#141c2a; border-radius:10px; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_client():
    cfg = Config.load()
    return DeltaClient(base_url=cfg.exchange.base_url,
                       api_key=cfg.exchange.api_key,
                       api_secret=cfg.exchange.api_secret), cfg


def contract_size(sym: str) -> float:
    if sym.startswith(("C-BTC", "P-BTC")) or sym == "BTCUSD":
        return BTC_CONTRACT
    if sym.startswith(("C-ETH", "P-ETH")) or sym == "ETHUSD":
        return ETH_CONTRACT
    return 1.0


@st.cache_data(ttl=25)
def fetch(_client: DeltaClient, have_keys: bool) -> dict:
    out = {"balance": None, "positions": [], "fills": [], "prices": {},
           "quotes": {}, "err": None}
    try:
        for sym in ("BTCUSD", "ETHUSD"):
            try:
                t = _client.get_ticker(sym)
                out["prices"][sym] = float(t.get("close") or t.get("mark_price") or 0) or None
            except Exception:
                pass
        if have_keys:
            usd = 0.0
            for b in _client.get_balances():
                if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
                    usd += float(b.get("balance", 0) or 0)
            out["balance"] = usd or None
            out["positions"] = _client.get_positions() or []
            out["fills"] = _client.get_fills(200) or []
            # Delta's position.unrealized_pnl is WRONG for options (returns -3.18
            # on a position actually down -11.45), so quote each open symbol and
            # compute it ourselves from the bid — what you would really receive.
            for p in out["positions"]:
                sym = str(p.get("product_symbol") or "")
                if not sym or int(float(p.get("size") or 0)) == 0:
                    continue
                try:
                    t = _client.get_ticker(sym)
                    q = t.get("quotes") or {}
                    out["quotes"][sym] = {
                        "mark": float(t.get("mark_price") or 0),
                        "bid": float(q.get("best_bid") or 0),
                        "ask": float(q.get("best_ask") or 0),
                    }
                except Exception:
                    pass
    except Exception as exc:
        out["err"] = str(exc)
    return out


def realised_trades(fills: list[dict]) -> list[dict]:
    """Group fills into ROUND TRIPS — one row per position, not per fill.

    A single order is often filled in pieces (a 74-lot exit came back as
    20 + 1 + 29 + 24). Matching fill-against-fill would report that as four
    separate trades and quarter the average trade size, so instead each symbol is
    tracked as a POSITION EPISODE: it opens when size leaves zero and closes when
    size returns to zero, and the whole episode is one trade. Entry/exit prices
    are size-weighted averages across every partial fill.
    """
    chrono = sorted(fills, key=lambda f: str(f.get("created_at") or ""))
    live: dict[str, dict] = {}
    trades = []
    for f in chrono:
        sym = f.get("product_symbol") or ""
        if not sym:
            continue
        try:
            qty = abs(float(f.get("size") or 0))
            px = float(f.get("price") or 0)
            comm = float(f.get("commission") or 0)
        except Exception:
            continue
        if qty <= 0 or px <= 0:
            continue
        sign = 1 if str(f.get("side", "")).lower() == "buy" else -1
        cs = contract_size(sym)

        ep = live.get(sym)
        if ep is None:
            ep = live[sym] = dict(pos=0.0, dir=sign, in_qty=0.0, in_val=0.0,
                                  out_qty=0.0, out_val=0.0, fees=0.0,
                                  opened=f.get("created_at"))
        ep["fees"] += comm
        if sign == ep["dir"]:                       # adding to the position
            ep["in_qty"] += qty
            ep["in_val"] += qty * px
        else:                                       # reducing / closing
            ep["out_qty"] += qty
            ep["out_val"] += qty * px
        ep["pos"] += sign * qty

        if abs(ep["pos"]) < 1e-9 and ep["out_qty"] > 0:   # flat -> book one trade
            entry = ep["in_val"] / ep["in_qty"] if ep["in_qty"] else 0.0
            exitp = ep["out_val"] / ep["out_qty"] if ep["out_qty"] else 0.0
            gross = (exitp - entry) * ep["out_qty"] * cs * ep["dir"]
            trades.append({
                "symbol": sym, "opened": ep["opened"], "closed": f.get("created_at"),
                "side": "LONG" if ep["dir"] > 0 else "SHORT",
                "size": ep["out_qty"], "entry": entry, "exit": exitp,
                "pnl": gross - ep["fees"], "fees": ep["fees"],
            })
            del live[sym]
    return trades


def load_baseline(balance):
    """Capital + start timestamp for THIS strategy. Created on first run."""
    if BASELINE_PATH.exists():
        try:
            d = json.loads(BASELINE_PATH.read_text())
            return float(d["starting_capital"]), str(d["strategy_start"])
        except Exception:
            pass
    start_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    cap = float(balance or 0.0)
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps({
        "starting_capital": round(cap, 4),
        "strategy_start": start_ts,
        "note": ("Everything on the dashboard counts only option fills at/after "
                 "strategy_start. Re-baseline with scripts/reset_dashboard.py."),
    }, indent=1))
    return cap, start_ts


def in_scope(f, start_iso: str) -> bool:
    """Fills from any ENABLED leg, on/after the baseline.

    Both option books and the BTC perp count now that v2/ema_rsi are live again.
    Anything else on the account is somebody else's trade and is excluded.
    """
    sym = str(f.get("product_symbol") or "")
    if not (asset_of(sym) or sym in ("BTCUSD", "ETHUSD")):
        return False
    return str(f.get("created_at") or "") >= start_iso


client, cfg = get_client()
have_keys = bool(cfg.exchange.api_key and cfg.exchange.api_secret)
data = fetch(client, have_keys)

bal = data["balance"]
baseline, strat_start = load_baseline(bal)
scoped_fills = [f for f in data["fills"] if in_scope(f, strat_start)]
trades = realised_trades(scoped_fills)
realised = sum(t["pnl"] for t in trades)

# Open positions must count too. Reporting +0.00% while an open trade is -$12
# is the kind of thing you only notice after trusting it.
open_pnl = 0.0
open_by_asset = {"BTC": 0.0, "ETH": 0.0, "BTC-perp": 0.0, "ETH-perp": 0.0}
for _p in data["positions"]:
    _sym = str(_p.get("product_symbol") or "")
    _sz = int(float(_p.get("size") or 0))
    _a = asset_of(_sym) or (f"{_sym[:3]}-perp" if _sym in ("BTCUSD", "ETHUSD") else "")
    if not _a or _sz == 0:
        continue
    _q = data["quotes"].get(_sym, {})
    _px = _q.get("bid") or _q.get("mark") or 0.0
    _e = float(_p.get("entry_price", 0) or 0)
    _u = abs(_sz) * (_px - _e) * contract_size(_sym) * (1 if _sz > 0 else -1)
    open_by_asset[_a] += _u
    open_pnl += _u

total_pnl = realised + open_pnl
ret_pct = (100.0 * total_pnl / baseline) if (baseline and baseline > 0) else None
try:
    start_label = dt.datetime.fromisoformat(strat_start).strftime("%d %b %Y")
except Exception:
    start_label = strat_start[:10]

# ---------- header ----------
top = st.columns([2.4, 1, 1])
with top[0]:
    st.markdown("## Delta Bot Monitor")
    st.markdown(f'<span class="dim">options_dip · BTC &nbsp;+&nbsp; ut_stc · ETH '
                f'— <b>ACTIVE</b> &nbsp;·&nbsp; perp legs disabled<br>'
                f'<span style="font-size:12px">counting option trades only, '
                f'since <b>{start_label}</b></span></span>', unsafe_allow_html=True)
with top[1]:
    st.markdown('<div class="lbl">Account value</div>', unsafe_allow_html=True)
    # cash alone understates you while a position is open: buying an option moves
    # the premium out of cash into the position.
    equity = (bal or 0.0) + sum(
        abs(int(float(p.get("size") or 0)))
        * ((data["quotes"].get(str(p.get("product_symbol")), {}).get("bid")
            or data["quotes"].get(str(p.get("product_symbol")), {}).get("mark") or 0.0))
        * contract_size(str(p.get("product_symbol") or ""))
        for p in data["positions"] if int(float(p.get("size") or 0)) != 0)
    st.markdown(f'<div class="kpi mono">{"$%.2f" % equity if bal is not None else "—"}</div>'
                + (f'<div class="dim mono" style="font-size:11.5px">'
                   f'cash ${bal:,.2f} + open ${equity-bal:,.2f}</div>'
                   if bal is not None and abs(equity - bal) > 0.005 else ''),
                unsafe_allow_html=True)
with top[2]:
    st.markdown('<div class="lbl">Return · this strategy</div>', unsafe_allow_html=True)
    if ret_pct is None:
        st.markdown('<div class="kpi mono dim">—</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            f'<div class="kpi mono {"up" if ret_pct >= 0 else "down"}">'
            f'{"+" if ret_pct >= 0 else "−"}{abs(ret_pct):.2f}%</div>'
            f'<div class="dim mono" style="font-size:11.5px">'
            f'{"+" if total_pnl >= 0 else "−"}${abs(total_pnl):,.2f} on ${baseline:,.2f} '
            f'since {start_label}'
            + (f'<br>realised {realised:+,.2f} · open {open_pnl:+,.2f}'
               if abs(open_pnl) > 0.005 else '') + '</div>',
            unsafe_allow_html=True)

if not have_keys:
    st.info("Read-only mode — add DELTA_API_KEY / DELTA_API_SECRET to .env for "
            "balance, positions and P&L.")
if data["err"]:
    st.warning(f"Delta API: {data['err']}")

st.caption(f"Updated {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · cached 25s · "
           f"press R or the button to refresh · scope: BTC + ETH options since "
           f"{start_label} ({len(scoped_fills)} fills counted, perp trades excluded)")
if st.button("↻ Refresh now"):
    st.cache_data.clear()
    st.rerun()

# ---------- realised stats ----------
if trades:
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    cells = st.columns(5)
    stats = [
        ("Closed trades", f"{len(trades)}", ""),
        ("Win rate", f"{100*len(wins)/len(trades):.0f}%", ""),
        ("Realised P&L", f"{'+' if realised>=0 else '−'}${abs(realised):,.2f}",
         "up" if realised >= 0 else "down"),
        ("Avg / trade", f"{'+' if realised>=0 else '−'}${abs(realised)/len(trades):,.2f}",
         "up" if realised >= 0 else "down"),
        ("Profit factor", f"{(gw/gl):.2f}" if gl > 0 else "—", ""),
    ]
    for c, (lbl, val, cls) in zip(cells, stats):
        with c:
            st.markdown(f'<div class="statbox"><div class="lbl">{lbl}</div>'
                        f'<div class="kpi sm mono {cls}">{val}</div></div>',
                        unsafe_allow_html=True)

# ---------- per-asset breakdown ----------
# BTC and ETH run different strategies on different contracts. A combined number
# tells you the account is up or down; it does not tell you which leg did it.
st.markdown("")
st.markdown("#### By asset")
_acols = st.columns(2)
for _c, _meta in zip(_acols, ACTIVE):
    _a = _meta["asset"]
    # Use each leg's own match(), so an options leg and a perp leg are both
    # attributed correctly rather than assuming everything is an option.
    _tr = [t for t in trades if _meta["match"](str(t.get("symbol") or ""))]
    _real = sum(t["pnl"] for t in _tr)
    _open = 0.0
    for _p in data["positions"]:
        _s = str(_p.get("product_symbol") or "")
        _z = int(float(_p.get("size") or 0))
        if _z == 0 or not _meta["match"](_s):
            continue
        _q2 = data["quotes"].get(_s, {})
        _x = _q2.get("bid") or _q2.get("mark") or 0.0
        _e2 = float(_p.get("entry_price", 0) or 0)
        _open += abs(_z) * (_x - _e2) * contract_size(_s) * (1 if _z > 0 else -1)
    _tot = _real + _open
    _wins = [t for t in _tr if t["pnl"] > 0]
    _wr = f"{100*len(_wins)/len(_tr):.0f}%" if _tr else "—"
    _avg = f"${_real/len(_tr):+,.2f}" if _tr else "—"
    _ret = f"{100*_tot/baseline:+.2f}%" if (baseline and baseline > 0) else "—"
    _cls = "up" if _tot >= 0 else "down"
    with _c:
        st.markdown(
            f'<div class="card" style="--sc:{_meta["color"]}">'
            f'<div style="display:flex;align-items:center;gap:9px;margin-bottom:12px">'
            f'<span class="tok" style="background:{_meta["color"]}">{_meta["tok"]}</span>'
            f'<div><div style="font-weight:640;font-size:15px">{_a} options'
            f' <span class="dim" style="font-weight:400">· {_meta["name"]}</span></div>'
            f'<div class="dim mono" style="font-size:11.5px">{_meta["market"]}</div>'
            f'</div></div>'
            f'<div style="display:flex;gap:22px;flex-wrap:wrap">'
            f'<div><div class="lbl">Total P&amp;L</div>'
            f'<div class="kpi sm mono {_cls}">{"+" if _tot>=0 else "−"}'
            f'${abs(_tot):,.2f}</div></div>'
            f'<div><div class="lbl">Return</div>'
            f'<div class="kpi sm mono {_cls}">{_ret}</div></div>'
            f'<div><div class="lbl">Trades</div>'
            f'<div class="kpi sm mono">{len(_tr)}</div></div>'
            f'<div><div class="lbl">Win rate</div>'
            f'<div class="kpi sm mono">{_wr}</div></div>'
            f'<div><div class="lbl">Avg / trade</div>'
            f'<div class="kpi sm mono">{_avg}</div></div>'
            f'</div>'
            f'<div class="dim mono" style="font-size:11.5px;margin-top:10px">'
            f'realised {_real:+,.2f} · open {_open:+,.2f}</div>'
            f'</div>', unsafe_allow_html=True)

# ---------- strategy cards ----------
st.markdown("")
open_pos = [p for p in data["positions"] if int(float(p.get("size") or 0)) != 0]


def card(meta, active: bool):
    mine = [p for p in open_pos if meta["match"](str(p.get("product_symbol") or ""))]
    if mine:
        p = mine[0]
        size = int(float(p.get("size") or 0))
        side = "long" if size > 0 else "short"
        entry = float(p.get("entry_price", 0) or 0)
        # compute it — never trust p["unrealized_pnl"] for options
        qt = data["quotes"].get(str(p.get("product_symbol") or ""), {})
        exitpx = qt.get("bid") or qt.get("mark") or 0.0
        cs = contract_size(str(p.get("product_symbol") or ""))
        upnl = abs(size) * (exitpx - entry) * cs * (1 if size > 0 else -1)
        body = (f'<span class="pill {side}">{side.upper()} {abs(size)} lots</span>'
                f'<span class="dim mono" style="margin-left:8px">'
                f'{p.get("product_symbol")} @ {entry:,.1f}</span>'
                f'<div class="{"up" if upnl>=0 else "down"} mono" '
                f'style="font-size:22px;margin-top:8px">{"+" if upnl>=0 else "−"}'
                f'${abs(upnl):,.2f} <span class="dim" style="font-size:12px">'
                f'unrealized</span></div>')
    elif active:
        body = ('<span class="pill flat">FLAT</span>'
                '<div class="dim mono" style="font-size:14px;margin-top:8px">'
                'no open position · waiting for a signal</div>')
    else:
        body = ('<span class="pill off">DISABLED</span>'
                '<div class="dim mono" style="font-size:14px;margin-top:8px">'
                'dry_run: true — not trading</div>')
    st.markdown(
        f'<div class="card {"" if active else "off"}" style="--sc:{meta["color"]}">'
        f'<div style="display:flex;align-items:center;gap:9px;margin-bottom:10px">'
        f'<span class="tok" style="background:{meta["color"]}">{meta["tok"]}</span>'
        f'<div><div style="font-weight:640;font-size:15px">{meta["name"]}</div>'
        f'<div class="dim mono" style="font-size:11.5px">{meta["market"]}</div></div></div>'
        f'{body}</div>', unsafe_allow_html=True)


st.markdown("#### Open positions")
cols = st.columns(2)
for c, meta in zip(cols, ACTIVE):
    with c:
        card(meta, True)
cols = st.columns(2)
for c, meta in zip(cols, DISABLED):
    with c:
        card(meta, False)

# ---------- trade history ----------
st.markdown("#### Trade history")
if trades:
    def fmt(x):
        try:
            return pd.to_datetime(x).strftime("%b %d %H:%M")
        except Exception:
            return str(x)
    rows = [{
        "Closed": fmt(t["closed"]),
        "Instrument": t["symbol"],
        "Side": t["side"],
        "Size": f'{t["size"]:g}',
        "Entry": f'{t["entry"]:,.1f}',
        "Exit": f'{t["exit"]:,.1f}',
        "Fees": f'${t["fees"]:.2f}',
        "P&L": f'{"+" if t["pnl"]>=0 else "−"}${abs(t["pnl"]):,.2f}',
    } for t in sorted(trades, key=lambda z: str(z["closed"]), reverse=True)]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"{len(trades)} closed round trips · P&L computed by FIFO-matching fills "
               f"(Delta's API returns no realized_pnl) · commissions included")
elif have_keys:
    st.markdown(f'<span class="dim">No closed trades yet for this strategy '
                f'(counting from {start_label}). Open positions show in the card above; '
                f'P&L is booked here once they close.</span>', unsafe_allow_html=True)
else:
    st.markdown('<span class="dim">Connect API keys to see trade history.</span>',
                unsafe_allow_html=True)

st.caption(f"Read-only — never places or cancels orders. Scope and capital baseline live "
           f"in data/dashboard_baseline.json (start {start_label}, capital "
           f"${baseline:,.2f}). Run `py scripts/reset_dashboard.py` to restart the "
           f"counter from now.")
