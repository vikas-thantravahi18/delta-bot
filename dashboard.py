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

ACTIVE = {
    "name": "options_dip",
    "market": "BTC options · 5m signal · +400 pts · 10% stake",
    "tok": "◆", "color": "#f7931a",
    "match": lambda s: s.startswith(("C-BTC", "P-BTC")),
}
DISABLED = [
    {"name": "v2 + ema_rsi", "market": "BTCUSD · 1h + 30m", "tok": "₿",
     "color": "#3d4759", "match": lambda s: s == "BTCUSD"},
    {"name": "ut_stc", "market": "ETHUSD · 4h", "tok": "Ξ",
     "color": "#3d4759", "match": lambda s: s == "ETHUSD"},
]

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
    out = {"balance": None, "positions": [], "fills": [], "prices": {}, "err": None}
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
    except Exception as exc:
        out["err"] = str(exc)
    return out


def realised_trades(fills: list[dict]) -> list[dict]:
    """FIFO-match fills per symbol into CLOSED round trips with computed P&L."""
    chrono = sorted(fills, key=lambda f: str(f.get("created_at") or ""))
    books: dict[str, deque] = defaultdict(deque)
    trades = []
    for f in chrono:
        sym = f.get("product_symbol") or ""
        if not sym:
            continue
        try:
            size = abs(float(f.get("size") or 0))
            px = float(f.get("price") or 0)
            comm = float(f.get("commission") or 0)
        except Exception:
            continue
        if size <= 0 or px <= 0:
            continue
        sign = 1 if str(f.get("side", "")).lower() == "buy" else -1
        cs = contract_size(sym)
        book = books[sym]
        comm_pu = comm / size if size else 0.0

        while size > 0 and book and book[0]["sign"] != sign:
            lot = book[0]
            qty = min(size, lot["size"])
            direction = lot["sign"]
            gross = (px - lot["px"]) * qty * cs * direction
            fees = qty * (lot["comm"] + comm_pu)
            trades.append({
                "symbol": sym, "opened": lot["t"], "closed": f.get("created_at"),
                "side": "LONG" if direction > 0 else "SHORT", "size": qty,
                "entry": lot["px"], "exit": px, "pnl": gross - fees, "fees": fees,
            })
            lot["size"] -= qty
            size -= qty
            if lot["size"] <= 1e-12:
                book.popleft()
        if size > 0:
            book.append({"sign": sign, "size": size, "px": px,
                         "comm": comm_pu, "t": f.get("created_at")})
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
    """Only this strategy's trades: option instruments, on/after the start."""
    sym = str(f.get("product_symbol") or "")
    if not sym.startswith(("C-BTC", "P-BTC")):
        return False
    ts = str(f.get("created_at") or "")
    return ts >= start_iso


client, cfg = get_client()
have_keys = bool(cfg.exchange.api_key and cfg.exchange.api_secret)
data = fetch(client, have_keys)

bal = data["balance"]
baseline, strat_start = load_baseline(bal)
scoped_fills = [f for f in data["fills"] if in_scope(f, strat_start)]
trades = realised_trades(scoped_fills)
realised = sum(t["pnl"] for t in trades)
ret_pct = (100.0 * realised / baseline) if (baseline and baseline > 0) else None
try:
    start_label = dt.datetime.fromisoformat(strat_start).strftime("%d %b %Y")
except Exception:
    start_label = strat_start[:10]

# ---------- header ----------
top = st.columns([2.4, 1, 1])
with top[0]:
    st.markdown("## Delta Bot Monitor")
    st.markdown(f'<span class="dim">options_dip · BTC options — <b>ACTIVE</b> '
                f'&nbsp;·&nbsp; perp legs disabled<br>'
                f'<span style="font-size:12px">counting this strategy only, '
                f'since <b>{start_label}</b></span></span>', unsafe_allow_html=True)
with top[1]:
    st.markdown('<div class="lbl">Account balance</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="kpi mono">{"$%.2f" % bal if bal is not None else "—"}</div>',
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
            f'{"+" if realised >= 0 else "−"}${abs(realised):,.2f} on ${baseline:,.2f} '
            f'since {start_label}</div>',
            unsafe_allow_html=True)

if not have_keys:
    st.info("Read-only mode — add DELTA_API_KEY / DELTA_API_SECRET to .env for "
            "balance, positions and P&L.")
if data["err"]:
    st.warning(f"Delta API: {data['err']}")

st.caption(f"Updated {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · cached 25s · "
           f"press R or the button to refresh · scope: options_dip since {start_label} "
           f"({len(scoped_fills)} fills counted, older/perp trades excluded)")
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
        upnl = float(p.get("unrealized_pnl", 0) or 0)
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


card(ACTIVE, True)
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
