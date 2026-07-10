"""Live monitoring dashboard for the portfolio.

  pip install streamlit
  streamlit run dashboard.py

Reads your Delta account (balance, open positions, recent fills) and attributes
every trade to its BOOK by MARKET — BTCUSD -> v2 + ema_rsi, ETHUSD -> ut_stc.
The two BTC strategies share one net position and the exchange fills carry no
strategy tag, so BTC trades are shown as one combined book (they can't be split
apart after the fact). Works even before any trade (shows balance + status).
Needs DELTA_API_KEY / DELTA_API_SECRET in .env for account data; without them it
shows public prices only.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src.config import Config
from src.exchange import DeltaClient

# map market -> book identity (BTC hosts two strategies on one shared position)
LEGS = {
    "BTCUSD": {"name": "v2 + ema_rsi", "market": "BTCUSD · 1h + 30m", "tok": "₿", "color": "#f7931a"},
    "ETHUSD": {"name": "ut_stc", "market": "ETHUSD · 4h", "tok": "Ξ", "color": "#7d8cf2"},
}

st.set_page_config(page_title="Delta Bot Monitor", page_icon="📊", layout="wide")

st.markdown("""
<style>
  .stApp { background:#0c121d; }
  .block-container { padding-top:1.4rem; max-width:1180px; }
  h1,h2,h3,h4,p,span,div,td,th { color:#e7ecf4; }
  .mono { font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-variant-numeric:tabular-nums; }
  .card { background:#141c2a; border:1px solid #27313f; border-radius:14px; padding:16px 18px; margin-bottom:6px; position:relative; overflow:hidden; }
  .card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--sc); }
  .kpi { font-size:30px; font-weight:600; letter-spacing:-.02em; }
  .lbl { font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#64708a; }
  .up { color:#33cd7c; } .down { color:#ec5b62; } .dim { color:#97a3b6; }
  .pill { display:inline-block; font-size:11.5px; font-weight:650; padding:3px 10px; border-radius:100px; border:1px solid #27313f; color:#97a3b6; }
  .pill.long { color:#33cd7c; border-color:#2e6b4e; } .pill.short { color:#ec5b62; border-color:#7a3238; }
  .pill.flat { color:#97a3b6; }
  .tok { display:inline-grid; place-items:center; width:24px; height:24px; border-radius:50%; color:#fff; font-weight:700; font-size:12px; }
  [data-testid="stDataFrame"] { background:#141c2a; border-radius:10px; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_client() -> tuple[DeltaClient, Config]:
    cfg = Config.load()
    return DeltaClient(base_url=cfg.exchange.base_url,
                       api_key=cfg.exchange.api_key,
                       api_secret=cfg.exchange.api_secret), cfg


@st.cache_data(ttl=25)
def fetch(_client: DeltaClient, have_keys: bool) -> dict:
    out = {"balance": None, "positions": [], "fills": [], "prices": {}, "err": None}
    try:
        for sym in LEGS:
            try:
                t = _client.get_ticker(sym)
                out["prices"][sym] = float(t.get("close") or t.get("mark_price") or 0) or None
            except Exception:
                pass
        if have_keys:
            bals = _client.get_balances()
            usd = 0.0
            for b in bals:
                if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
                    usd += float(b.get("balance", 0) or 0)
            out["balance"] = usd or (float(bals[0].get("balance", 0)) if bals else None)
            out["positions"] = _client.get_positions() or []
            out["fills"] = _client.get_fills(100) or []
    except Exception as exc:
        out["err"] = str(exc)
    return out


def strat_of(symbol: str) -> str:
    return LEGS.get(symbol, {}).get("name", symbol)


client, cfg = get_client()
have_keys = bool(cfg.exchange.api_key and cfg.exchange.api_secret)
data = fetch(client, have_keys)

# ---------- header ----------
top = st.columns([3, 1])
with top[0]:
    st.markdown("## Delta Bot Monitor")
    st.markdown('<span class="dim">v2 + ema_rsi · BTCUSD &nbsp;+&nbsp; ut_stc · ETHUSD — 3-strategy portfolio</span>',
                unsafe_allow_html=True)
with top[1]:
    bal = data["balance"]
    st.markdown('<div class="lbl">Account balance</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="kpi mono">{"$%.2f" % bal if bal is not None else "—"}</div>',
                unsafe_allow_html=True)

if not have_keys:
    st.info("Read-only mode — add DELTA_API_KEY / DELTA_API_SECRET to .env to see balance, positions and trades. Live prices shown below.")
if data["err"]:
    st.warning(f"Delta API: {data['err']}")

st.caption(f"Updated {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
           f"data cached 25s · press R or the button to refresh")
if st.button("↻ Refresh now"):
    st.cache_data.clear()
    st.rerun()

# ---------- per-strategy cards ----------
pos_by_sym = {}
for p in data["positions"]:
    sym = p.get("product_symbol") or (p.get("product") or {}).get("symbol")
    if sym in LEGS and int(p.get("size", 0) or 0) != 0:
        pos_by_sym[sym] = p

cols = st.columns(2)
for col, (sym, meta) in zip(cols, LEGS.items()):
    p = pos_by_sym.get(sym)
    price = data["prices"].get(sym)
    with col:
        if p:
            size = int(p.get("size", 0) or 0)
            side = "long" if size > 0 else "short"
            entry = float(p.get("entry_price", 0) or 0)
            upnl = float(p.get("unrealized_pnl", 0) or 0)
            pos_html = (f'<span class="pill {side}">{side.upper()} {abs(size)} lots</span>'
                        f'<span class="dim mono" style="margin-left:8px">@ {entry:,.2f}</span>')
            upnl_html = f'<div class="{ "up" if upnl>=0 else "down"} mono" style="font-size:22px;margin-top:8px">{"+" if upnl>=0 else "−"}${abs(upnl):,.2f} <span class="dim" style="font-size:12px">unrealized</span></div>'
        else:
            pos_html = '<span class="pill flat">FLAT</span>'
            upnl_html = f'<div class="dim mono" style="font-size:14px;margin-top:8px">no open position · mark {("$%.2f"%price) if price else "—"}</div>'
        st.markdown(
            f'<div class="card" style="--sc:{meta["color"]}">'
            f'<div style="display:flex;align-items:center;gap:9px;margin-bottom:10px">'
            f'<span class="tok" style="background:{meta["color"]}">{meta["tok"]}</span>'
            f'<div><div style="font-weight:640;font-size:15px">{meta["name"]}</div>'
            f'<div class="dim mono" style="font-size:11.5px">{meta["market"]}</div></div></div>'
            f'{pos_html}{upnl_html}</div>',
            unsafe_allow_html=True,
        )

# ---------- fills / trade history ----------
st.markdown("#### Trade history")
fills = data["fills"]
if fills:
    rows = []
    for f in fills:
        sym = f.get("product_symbol") or (f.get("product") or {}).get("symbol")
        if sym not in LEGS:
            continue
        ts = f.get("created_at") or f.get("timestamp")
        try:
            ts = pd.to_datetime(ts).strftime("%b %d %H:%M")
        except Exception:
            ts = str(ts)
        rows.append({
            "Time": ts,
            "Strategy": strat_of(sym),
            "Market": sym,
            "Side": str(f.get("side", "")).upper(),
            "Size": f.get("size"),
            "Price": f.get("price"),
            "P&L": f.get("realized_pnl") or f.get("pnl") or "",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.markdown('<span class="dim">No fills on BTCUSD/ETHUSD yet.</span>', unsafe_allow_html=True)
elif have_keys:
    st.markdown('<span class="dim">No trades yet — the strategies are waiting for a signal.</span>',
                unsafe_allow_html=True)
else:
    st.markdown('<span class="dim">Connect API keys to see trade history.</span>', unsafe_allow_html=True)

st.caption("Attribution by market: BTCUSD → v2 + ema_rsi (one shared book), ETHUSD → ut_stc. "
           "This dashboard is read-only — it never places or cancels orders.")
