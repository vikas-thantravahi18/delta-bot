"""BTC OPTIONS live trader — Trend Dip Sniper traded through the option chain.

HOW A TRADE WORKS
-----------------
1. SIGNAL   evaluated on closed 5-minute BTCUSD candles (options_dip strategy).
2. EXPIRY   Delta BTC options settle at 12:00 UTC daily. Pick TODAY'S expiry;
            if under `min_hours_to_expiry` remain, SKIP the signal (theta is
            brutal in the last hours and the option can die before the target).
3. STRIKE   at-the-money — the listed strike nearest to spot. CALL for a long
            signal, PUT for a short one.
4. SIZE     stake = `stake_pct` of the wallet (default 10%).
            lots  = floor(stake / (premium_per_BTC x contract_value)).
            Percentage sizing is deliberate: a fixed stake cannot shrink during a
            losing run, and real-sequence testing put its ruin risk near 20%.
5. EXIT     there is NO stop-loss. The premium already caps the loss. The trade
            closes when EITHER
              * BTC moves `target_points` in our favour  -> sell to close, or
              * we reach `close_before_expiry_min` before settlement -> sell to close.
            The target is on the UNDERLYING, not the premium, because the premium
            that corresponds to "+400 BTC points" changes with time and IV. The
            bot therefore polls BTC and closes at market.

STATE / RESTART SAFETY
----------------------
The open trade is written to data/options_state.json after every change, and on
startup the bot reconciles that file against the exchange's real positions. If a
position exists that the file does not know about, the bot adopts it and manages
it to expiry rather than leaving it unmanaged.

DRY-RUN by default. Real orders need live=True AND live.dry_run=false.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from ..config import Config
from ..data.loader import RESOLUTION_SECONDS, load_candles
from ..exchange import DeltaClient
from ..strategies.options_dip import OptionsDipStrategy
from .notifier import TelegramNotifier

log = logging.getLogger("options")

STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "options_state.json"
SETTLE_HOUR_UTC = 12


@dataclass
class OpenTrade:
    symbol: str                 # option symbol, e.g. C-BTC-63000-030826
    product_id: int
    side: str                   # "long" (call) / "short" (put)
    lots: int
    entry_premium: float        # per-BTC quote at entry
    stake_usd: float
    btc_entry: float
    btc_target: float
    expiry_iso: str
    opened_iso: str

    def to_json(self) -> dict:
        return asdict(self)


class OptionsTrader:
    def __init__(self, cfg: Config, live: bool = False) -> None:
        self.cfg = cfg
        self.client = DeltaClient(
            base_url=cfg.exchange.base_url,
            api_key=cfg.exchange.api_key,
            api_secret=cfg.exchange.api_secret,
        )
        p = cfg.strategy.params or {}
        self.strategy = OptionsDipStrategy(
            ema_fast=p.get("ema_fast", 50), ema_slow=p.get("ema_slow", 200),
            rsi_len=p.get("rsi_len", 14), rsi_buy=p.get("rsi_buy", 40.0),
            rsi_sell=p.get("rsi_sell", 60.0),
            target_points=p.get("target_points", 400.0),
            both_sides=p.get("both_sides", True),
        )
        self.target_points = float(p.get("target_points", 400.0))
        self.stake_pct = float(p.get("stake_pct", 0.10))
        self.min_hours_to_expiry = float(p.get("min_hours_to_expiry", 3.0))
        self.close_before_expiry_min = float(p.get("close_before_expiry_min", 20.0))
        self.cooldown_hours = float(p.get("cooldown_hours", 12.0))
        self.max_iv = float(p.get("max_iv", 0.30))
        self.max_spread_pct = float(p.get("max_spread_pct", 12.0))
        self.entry_cross_pct = float(p.get("entry_cross_pct", 0.15))
        self.entry_fill_seconds = int(p.get("entry_fill_seconds", 45))
        self.strike_pool = int(p.get("strike_pool", 3))
        self.underlying = p.get("underlying", "BTCUSD")

        self.dry_run = cfg.live.dry_run or not live
        self.notifier = TelegramNotifier(
            token=cfg.notify.telegram_token, chat_id=cfg.notify.telegram_chat_id,
            enabled=cfg.notify.enabled,
        )
        self.open_trade: Optional[OpenTrade] = None
        self.last_exit_iso: Optional[str] = None
        self.last_entry_iso: Optional[str] = None
        self._last_bar_time = None
        self._trades_today = 0
        self._today = None

    # ================================================================== #
    # state
    # ================================================================== #
    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            d = json.loads(STATE_PATH.read_text())
        except Exception as exc:
            log.warning("Could not read state file (%s); starting clean.", exc)
            return
        self.last_exit_iso = d.get("last_exit_iso")
        self.last_entry_iso = d.get("last_entry_iso")
        ot = d.get("open_trade")
        if ot:
            self.open_trade = OpenTrade(**ot)
            log.info("Restored open trade from state: %s x%d", ot["symbol"], ot["lots"])

    def _save_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "open_trade": self.open_trade.to_json() if self.open_trade else None,
            "last_exit_iso": self.last_exit_iso,
            "last_entry_iso": self.last_entry_iso,
            "updated": datetime.now(timezone.utc).isoformat(),
        }, indent=1))

    # ================================================================== #
    # setup / reconcile
    # ================================================================== #
    def setup(self) -> None:
        self._load_state()
        log.info("BTC OPTIONS trader | target +%.0f pts | stake %.0f%% of wallet | "
                 "skip if <%.1fh to expiry | %s",
                 self.target_points, 100 * self.stake_pct, self.min_hours_to_expiry,
                 "DRY-RUN (no orders)" if self.dry_run else "LIVE")
        if self.dry_run:
            log.warning("DRY-RUN: no real orders will be placed.")
            return
        bal = self._balance()
        log.info("LIVE. Wallet $%.2f -> stake $%.2f per trade", bal, bal * self.stake_pct)
        self._reconcile()

    def _reconcile(self) -> None:
        """Make our state agree with the exchange's real option positions."""
        try:
            positions = self.client.get_option_positions()
        except Exception as exc:
            log.warning("Could not fetch option positions (%s).", exc)
            return
        live_syms = {p["product_symbol"]: p for p in positions}

        if self.open_trade and self.open_trade.symbol not in live_syms:
            log.warning("State had %s open but the exchange shows none — clearing.",
                        self.open_trade.symbol)
            self.open_trade = None
            self.last_exit_iso = datetime.now(timezone.utc).isoformat()
            self._save_state()

        for sym, p in live_syms.items():
            if self.open_trade and self.open_trade.symbol == sym:
                continue
            log.warning("ADOPTING untracked option position %s size=%s — will manage "
                        "it to expiry.", sym, p.get("size"))
            try:
                prod = self.client.get_product(sym)
                spot = float(self.client.get_ticker(self.underlying)["mark_price"])
                is_call = sym.startswith("C-")
                self.open_trade = OpenTrade(
                    symbol=sym, product_id=int(prod["id"]),
                    side="long" if is_call else "short",
                    lots=abs(int(float(p.get("size") or 0))),
                    entry_premium=float(p.get("entry_price") or 0.0),
                    stake_usd=0.0, btc_entry=spot,
                    btc_target=spot + (self.target_points if is_call else -self.target_points),
                    expiry_iso=str(prod.get("settlement_time")),
                    opened_iso=datetime.now(timezone.utc).isoformat(),
                )
                self._save_state()
            except Exception as exc:
                log.error("Could not adopt %s (%s).", sym, exc)

    # ================================================================== #
    # main loop
    # ================================================================== #
    def run_forever(self) -> None:
        self.setup()
        log.info("Loop started: %ss poll.", self.cfg.live.poll_seconds)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                log.info("Interrupted. Exiting.")
                break
            except Exception as exc:
                log.exception("tick error: %s", exc)
            time.sleep(self.cfg.live.poll_seconds)

    def tick(self) -> None:
        # an open trade is managed on EVERY tick, not once per candle
        if self.open_trade is not None:
            self._manage_open()
            return
        self._look_for_entry()

    # ------------------------------------------------------------------ #
    def _manage_open(self) -> None:
        t = self.open_trade
        spot = self._spot()
        if spot is None:
            return
        hit = (spot >= t.btc_target) if t.side == "long" else (spot <= t.btc_target)
        expiry = datetime.fromisoformat(t.expiry_iso.replace("Z", "+00:00"))
        mins_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 60.0

        prem = self._premium(t.symbol)
        pnl = ((prem - t.entry_premium) * t.lots * 0.001) if prem else 0.0
        log.info("HOLDING %s x%d | BTC %.1f -> target %.1f (%+.0f pts to go) | "
                 "premium %.1f (entry %.1f, P&L $%+.2f) | %.0f min to expiry",
                 t.symbol, t.lots, spot, t.btc_target,
                 (t.btc_target - spot) if t.side == "long" else (spot - t.btc_target),
                 prem or 0.0, t.entry_premium, pnl, mins_left)

        if hit:
            self._close("TARGET HIT (+%.0f BTC pts)" % self.target_points)
        elif mins_left <= self.close_before_expiry_min:
            self._close("EXPIRY in %.0f min" % mins_left)

    # ------------------------------------------------------------------ #
    def _look_for_entry(self) -> None:
        if self._cooling_down():
            return
        df = self._candles()
        if df is None or len(df) < self.strategy.warmup + 2:
            log.debug("Not enough candles yet.")
            return
        i = len(df) - 2                       # last CLOSED bar
        bar_time = df.index[i]
        if self._last_bar_time is not None and bar_time <= self._last_bar_time:
            return
        self._last_bar_time = bar_time

        self._roll_day(bar_time)
        if self._trades_today >= self.cfg.risk.max_trades_per_day:
            log.info("Daily trade cap reached (%s).", self.cfg.risk.max_trades_per_day)
            return

        prepared = self.strategy.prepare(df)
        sig = self.strategy.signal(prepared, i)
        if sig is None:
            log.info("[%s] no signal (BTC %.1f, RSI %.1f)",
                     bar_time, float(df.iloc[i]["close"]), float(prepared.iloc[i]["rsi"]))
            return
        log.info("SIGNAL %s | %s", sig.side.upper(), sig.reason)

        hrs = self._hours_to_settlement()
        if hrs < self.min_hours_to_expiry:
            log.info("SKIPPED: only %.1fh to the 12:00 UTC settlement "
                     "(need >= %.1fh).", hrs, self.min_hours_to_expiry)
            return

        pick = self._pick_option(sig.side, hrs)
        if pick is None:
            return
        self._enter(sig, pick)

    # ------------------------------------------------------------------ #
    def _pick_option(self, side: str, hours_left: float) -> Optional[dict]:
        """Nearest-expiry ATM call/put, with an IV sanity gate."""
        try:
            chain = self.client.get_option_tickers("BTC")
        except Exception as exc:
            log.error("Could not fetch option chain (%s).", exc)
            return None
        if not chain:
            log.error("Empty option chain.")
            return None

        want_call = side == "long"
        now = datetime.now(timezone.utc)
        target_expiry = now.replace(hour=SETTLE_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target_expiry <= now:
            target_expiry += timedelta(days=1)
        tag = target_expiry.strftime("%d%m%y")

        spot = None
        cands = []
        for t in chain:
            sym = str(t.get("symbol", ""))
            if not sym.endswith(tag):
                continue
            if want_call != sym.startswith("C-"):
                continue
            try:
                k = float(t["strike_price"]); mark = float(t["mark_price"])
                if mark <= 0:
                    continue
                spot = float(t.get("spot_price") or spot or 0)
                cands.append((abs(k - spot), k, mark, t))
            except Exception:
                continue
        if not cands or not spot:
            log.error("No %s options found for expiry %s.", "call" if want_call else "put", tag)
            return None

        # Among the strikes closest to spot, take the one with the TIGHTEST book,
        # not strictly the nearest. Measured live: the exact-ATM strike is often the
        # widest (63,000 call 17.9% vs 63,200 call 5.7% at the same moment), and a
        # 200-point strike shift costs far less than 12 points of spread.
        cands.sort()
        pool = cands[:self.strike_pool]
        def _spread_of(c):
            q = c[3].get("quotes") or {}
            b, a = float(q.get("best_bid") or 0), float(q.get("best_ask") or 0)
            return (100.0 * (a - b) / c[2]) if (b > 0 and a > 0 and c[2] > 0) else 999.0
        pool.sort(key=_spread_of)
        _, strike, mark, tick = pool[0]
        iv = float(tick.get("mark_vol") or 0)
        if iv and iv > self.max_iv:
            log.info("SKIPPED: ATM IV %.1f%% is above the %.1f%% ceiling — option too "
                     "expensive for a %.0f-point target.",
                     100 * iv, 100 * self.max_iv, self.target_points)
            return None

        quotes = tick.get("quotes") or {}
        ask = float(quotes.get("best_ask") or 0) or mark
        bid = float(quotes.get("best_bid") or 0) or mark
        spread_pct = (100 * (ask - bid) / mark) if mark else 0.0
        return dict(symbol=tick["symbol"], strike=strike, mark=mark, ask=ask, bid=bid,
                    iv=iv, spot=spot, spread_pct=spread_pct, hours_left=hours_left,
                    delta=float((tick.get("greeks") or {}).get("delta") or 0))

    # ------------------------------------------------------------------ #
    def _enter(self, sig, pick: dict) -> None:
        balance = self._balance()
        stake = balance * self.stake_pct
        # pay the ask when buying at market; premium is quoted per 1 BTC
        prem_per_lot = pick["ask"] * 0.001
        if prem_per_lot <= 0:
            log.error("Bad premium for %s.", pick["symbol"])
            return
        lots = int(stake / prem_per_lot)
        if lots < 1:
            log.warning("SKIPPED: stake $%.2f (%.0f%% of $%.2f) buys 0 lots at $%.3f "
                        "per contract. Need ~$%.2f.",
                        stake, 100 * self.stake_pct, balance, prem_per_lot, prem_per_lot)
            return
        cost = lots * prem_per_lot
        spot = pick["spot"]
        target = spot + (self.target_points if sig.side == "long" else -self.target_points)

        # what the premium is worth if BTC reaches the target now (delta approximation,
        # ignoring the theta that will be paid getting there) — for the log only
        est_gain = abs(pick["delta"]) * self.target_points * 0.001 * lots

        msg = (f"{'BUY CALL' if sig.side == 'long' else 'BUY PUT'} {pick['symbol']} "
               f"x{lots} lots | premium {pick['ask']:.1f}/BTC = ${cost:.2f} "
               f"({100*cost/balance:.1f}% of ${balance:.2f}) | BTC {spot:.1f} -> "
               f"target {target:.1f} | IV {100*pick['iv']:.1f}% delta {pick['delta']:.2f} "
               f"spread {pick['spread_pct']:.1f}% | {pick['hours_left']:.1f}h to expiry | "
               f"est +${est_gain:.2f} at target")

        if self.dry_run:
            log.info("[DRY-RUN] would %s", msg)
            self._trades_today += 1
            self.last_entry_iso = datetime.now(timezone.utc).isoformat()
            # notify in dry-run too, so the Telegram wiring is proven before real money
            self.notifier.notify_trade("options_dip", sig.side, lots)
            return

        if pick["spread_pct"] > self.max_spread_pct:
            log.warning("SKIPPED: %s spread is %.1f%% (limit %.0f%%) — crossing it "
                        "would cost more than the trade is worth.",
                        pick["symbol"], pick["spread_pct"], self.max_spread_pct)
            return

        log.info("[LIVE] %s", msg)
        try:
            prod = self.client.get_product(pick["symbol"])
            pid = int(prod["id"])
        except Exception as exc:
            log.error("Could not resolve product for %s: %s", pick["symbol"], exc)
            return

        # Same-day option books can be 12-30% wide. Market-buying that would hand
        # away most of the edge (the backtest assumed a 2.6% cost), so the entry is
        # a LIMIT at mid + a small nudge, cancelled if it does not fill.
        # Cross `entry_cross_pct` of the way from MID toward the ask — not
        # mid x (1 + pct), which for any realistic spread lands above the ask and
        # silently degenerates into paying the full offer.
        mid = 0.5 * (pick["bid"] + pick["ask"])
        limit_px = mid + self.entry_cross_pct * (pick["ask"] - mid)
        limit_px = min(limit_px, pick["ask"])          # never bid above the ask
        tick = float(prod.get("tick_size") or 0.1)
        limit_px = round(round(limit_px / tick) * tick, 8)
        log.info("       limit buy @ %.2f (bid %.1f / mid %.1f / ask %.1f), "
                 "waiting up to %ds for a fill",
                 limit_px, pick["bid"], mid, pick["ask"], self.entry_fill_seconds)
        try:
            resp = self.client.place_order(
                product_id=pid, size=lots, side="buy",
                order_type="limit_order", limit_price=limit_px,
            )
            log.info("Order response: %s", resp)
        except Exception as exc:
            log.error("ENTRY FAILED for %s: %s", pick["symbol"], exc)
            return

        if not self._await_fill(pid, pick["symbol"]):
            log.warning("Limit not filled in %ds — cancelling and skipping this signal.",
                        self.entry_fill_seconds)
            try:
                self.client.cancel_all(pid)
            except Exception as exc:
                log.error("CANCEL FAILED for %s: %s — check the exchange manually.",
                          pick["symbol"], exc)
            return
        pick = {**pick, "ask": limit_px}               # record the real fill reference

        self.open_trade = OpenTrade(
            symbol=pick["symbol"], product_id=int(prod["id"]), side=sig.side, lots=lots,
            entry_premium=pick["ask"], stake_usd=cost, btc_entry=spot, btc_target=target,
            expiry_iso=str(prod.get("settlement_time")),
            opened_iso=datetime.now(timezone.utc).isoformat(),
        )
        self._trades_today += 1
        self.last_entry_iso = self.open_trade.opened_iso
        self._save_state()
        self.notifier.notify_trade("options_dip", sig.side, lots)

    # ------------------------------------------------------------------ #
    def _close(self, why: str) -> None:
        t = self.open_trade
        prem = self._premium(t.symbol) or 0.0
        pnl = (prem - t.entry_premium) * t.lots * 0.001
        msg = (f"CLOSE {t.symbol} x{t.lots} — {why} | premium {t.entry_premium:.1f} -> "
               f"{prem:.1f} | P&L ${pnl:+.2f}")
        if self.dry_run:
            log.info("[DRY-RUN] would %s", msg)
        else:
            log.info("[LIVE] %s", msg)
            try:
                self.client.place_order(
                    product_id=t.product_id, size=t.lots, side="sell",
                    order_type="market_order", reduce_only=True,
                )
            except Exception as exc:
                log.error("CLOSE FAILED for %s: %s — position left open, will retry "
                          "next tick.", t.symbol, exc)
                return
        self.notifier.notify_close("options_dip", pnl, why)
        self.open_trade = None
        self.last_exit_iso = datetime.now(timezone.utc).isoformat()
        self._save_state()

    # ================================================================== #
    # helpers
    # ================================================================== #
    def _candles(self):
        step = RESOLUTION_SECONDS[self.cfg.market.resolution]
        end = int(time.time())
        start = end - step * (self.strategy.warmup + 400)
        return load_candles(self.client, self.underlying, self.cfg.market.resolution,
                            start, end, use_cache=False)

    def _spot(self) -> Optional[float]:
        try:
            return float(self.client.get_ticker(self.underlying)["mark_price"])
        except Exception as exc:
            log.warning("Could not fetch spot (%s).", exc)
            return None

    def _premium(self, symbol: str) -> Optional[float]:
        try:
            return float(self.client.get_ticker(symbol)["mark_price"])
        except Exception:
            return None

    def _balance(self) -> float:
        if self.dry_run:
            return float(self.cfg.starting_balance)
        try:
            for b in self.client.get_balances():
                if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
                    return float(b.get("available_balance", b.get("balance", 0)))
        except Exception as exc:
            log.warning("Could not fetch balance (%s); using config value.", exc)
        return float(self.cfg.starting_balance)

    def _await_fill(self, product_id: int, symbol: str) -> bool:
        """Poll positions until the option shows up, or the window expires."""
        deadline = time.time() + self.entry_fill_seconds
        while time.time() < deadline:
            time.sleep(3)
            try:
                for p in self.client.get_option_positions():
                    if str(p.get("product_symbol")) == symbol and                             abs(int(float(p.get("size") or 0))) > 0:
                        log.info("       filled: size %s", p.get("size"))
                        return True
            except Exception as exc:
                log.warning("fill check failed (%s), retrying.", exc)
        return False

    @staticmethod
    def _hours_to_settlement() -> float:
        now = datetime.now(timezone.utc)
        s = now.replace(hour=SETTLE_HOUR_UTC, minute=0, second=0, microsecond=0)
        if s <= now:
            s += timedelta(days=1)
        return (s - now).total_seconds() / 3600.0

    def _cooling_down(self) -> bool:
        """Cooldown runs from the last ENTRY, not the last exit.

        The backtest counted 144 bars (12h) between SIGNALS, so measuring from the
        exit instead would add the ~6h average hold on top and cut the trade rate
        by roughly a third versus the tested behaviour.
        """
        if not self.last_entry_iso:
            return False
        last = datetime.fromisoformat(self.last_entry_iso)
        left = self.cooldown_hours - (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
        if left > 0:
            log.info("Cooldown: %.1fh until the next entry is allowed.", left)
            return True
        return False

    def _roll_day(self, ts: pd.Timestamp) -> None:
        day = ts.date()
        if self._today != day:
            self._today = day
            self._trades_today = 0
