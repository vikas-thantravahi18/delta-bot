"""Live / paper trading loop.

The bot evaluates the strategy once per *closed* candle and, on a fresh signal,
sizes the trade with the RiskManager and submits a bracket order (entry + stop
+ take-profit) to Delta.

SAFETY DESIGN
-------------
* dry_run=true (default) NEVER sends orders — it only logs what it *would* do.
* Even with dry_run=false the caller must pass live=True (the run script asks
  for typed confirmation first).
* The bot keeps at most `max_open_positions` and respects `max_trades_per_day`.
This is a starting framework, not financial advice. Test on testnet / small size.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from ..config import Config
from ..data.loader import RESOLUTION_SECONDS, attach_higher_tf_trend, load_candles
from ..exchange import DeltaClient
from ..risk import RiskManager
from ..strategies import build_strategy

log = logging.getLogger("live")


class LiveTrader:
    def __init__(self, cfg: Config, live: bool = False) -> None:
        self.cfg = cfg
        self.client = DeltaClient(
            base_url=cfg.exchange.base_url,
            api_key=cfg.exchange.api_key,
            api_secret=cfg.exchange.api_secret,
        )
        self.strategy = build_strategy(cfg.strategy.name, cfg.strategy.params)
        self.risk = RiskManager(
            cfg.risk, lot_size=cfg.market.lot_size, min_lots=cfg.market.min_lots
        )
        # Effective dry-run = config dry_run OR caller did not opt into live.
        self.dry_run = cfg.live.dry_run or not live
        self.product = None
        self._trades_today = 0
        self._today = None
        self._last_bar_time = None

    # ------------------------------------------------------------------ #
    def setup(self) -> None:
        self.product = self.client.get_product(self.cfg.market.symbol)
        pid = self.product.get("id")
        # Use the exchange's real contract size for lot rounding when available.
        cv = float(self.product.get("contract_value", self.risk.lot_size) or self.risk.lot_size)
        if cv > 0:
            self.risk.lot_size = cv
        self._base = self.cfg.market.symbol.replace("USDT", "").replace("USD", "") or "units"
        log.info("Trading %s (product_id=%s, 1 lot=%s %s) on %s",
                 self.cfg.market.symbol, pid, self.risk.lot_size, self._base, self.cfg.exchange.base_url)
        if self.dry_run:
            log.warning("DRY-RUN mode: no real orders will be placed.")
        else:
            self._set_leverage(pid)
            bal = self._available_balance()
            log.info("LIVE mode. Available balance ~ $%.2f", bal)

    def run_forever(self) -> None:
        self.setup()
        log.info("Starting loop: evaluating every %ss on closed %s candles.",
                 self.cfg.live.poll_seconds, self.cfg.market.resolution)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                log.info("Interrupted by user. Exiting.")
                break
            except Exception as exc:  # keep the loop alive on transient errors
                log.exception("tick error: %s", exc)
            time.sleep(self.cfg.live.poll_seconds)

    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        df = self._recent_candles()
        if df is None or len(df) < self.strategy.warmup + 2:
            log.debug("Not enough candles yet.")
            return

        # Only act once per newly-closed candle.
        last_closed_idx = len(df) - 2  # final row may be the still-forming candle
        last_closed_time = df.index[last_closed_idx]
        if self._last_bar_time is not None and last_closed_time <= self._last_bar_time:
            return
        self._last_bar_time = last_closed_time

        self._roll_day(last_closed_time)
        if self._trades_today >= self.cfg.risk.max_trades_per_day:
            log.info("Daily trade cap reached (%s).", self.cfg.risk.max_trades_per_day)
            return
        if self._has_open_position():
            log.debug("Position already open; skipping.")
            return

        prepared = self.strategy.prepare(df)
        sig = self.strategy.signal(prepared, last_closed_idx)
        if sig is None:
            log.info("[%s] no signal (close=%.2f).",
                     last_closed_time, float(df.iloc[last_closed_idx]["close"]))
            return

        if not self.risk.liquidation_buffer_ok(sig.entry, sig.stop):
            log.warning(
                "Signal (%s) skipped: stop distance too wide for %gx leverage "
                "(exchange liquidation could trigger before our stop-loss). "
                "entry=%.1f stop=%.1f. Lower max_leverage or tighten atr_stop_mult.",
                sig.side, self.cfg.risk.max_leverage, sig.entry, sig.stop,
            )
            return

        balance = self._available_balance()
        plan = self.risk.build_plan(sig.side, sig.entry, sig.stop, balance)
        if plan is None:
            allocated = balance * self.cfg.risk.capital_allocation_pct
            log.warning(
                "Signal (%s) skipped: can't size >= %d lot within risk budget. "
                "wallet=$%.2f allocated(50%%)=$%.2f risk_budget=$%.2f, lot=%s BTC. "
                "Account likely too small for the current stop distance.",
                sig.side, self.risk.min_lots, balance, allocated,
                self.risk.risk_amount(balance), self.risk.lot_size,
            )
            return

        self._execute(plan, sig.reason)
        self._trades_today += 1

    # ------------------------------------------------------------------ #
    def _execute(self, plan, reason: str) -> None:
        side = "buy" if plan.side == "long" else "sell"
        trail_part = f"trail={plan.trail_amount:.1f} " if plan.trail_amount else ""
        base = getattr(self, "_base", None) or self.cfg.market.symbol.replace("USDT", "").replace("USD", "")
        msg = (
            f"{plan.side.upper()} {self.cfg.market.symbol} | {plan.lots} lots "
            f"({plan.qty:.4f} {base}, notional ${plan.notional:.2f}, margin ${plan.margin_usd:.2f}) "
            f"entry~{plan.entry:.1f} stop={plan.stop:.1f} tp={plan.take_profit:.1f} {trail_part}"
            f"risk=${plan.risk_usd:.2f} | {reason}"
        )
        if self.dry_run:
            log.info("[DRY-RUN] would place bracket order: %s", msg)
            return

        log.info("[LIVE] placing bracket order: %s", msg)
        resp = self.client.place_bracket_order(
            product_id=self.product["id"],
            size=plan.lots,
            side=side,
            stop_loss_price=round(plan.stop, 1),
            take_profit_price=round(plan.take_profit, 1),
            order_type=self.cfg.live.order_type,
            trail_amount=round(plan.trail_amount, 1) if plan.trail_amount else None,
        )
        log.info("Order response: %s", resp)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _recent_candles(self):
        step = RESOLUTION_SECONDS[self.cfg.market.resolution]
        end = int(time.time())
        start = end - step * (self.strategy.warmup + 300)
        df = load_candles(
            self.client, self.cfg.market.symbol, self.cfg.market.resolution,
            start, end, use_cache=False,
        )
        if getattr(self.strategy, "use_higher_tf_filter", False):
            df = attach_higher_tf_trend(
                df, self.cfg.market.trend_resolution,
                ema_period=self.cfg.strategy.params.get("ema_trend", 50),
            )
        return df

    def _set_leverage(self, product_id: int) -> None:
        """Force the exchange's per-product leverage to match risk.max_leverage.

        Without this, Delta uses whatever leverage was last set for the
        product (e.g. from manual UI trading), which can be much higher than
        the config assumes and make the real liquidation price sit closer
        to entry than the ATR-based stop — i.e. the position gets liquidated
        before our own stop-loss fires.

        In live mode this is a HARD requirement: if we can neither set the
        leverage nor confirm it already matches, we ABORT startup rather than
        trade with an unknown leverage. (Delta blocks leverage changes while a
        position is open, so we fall back to verifying the current value.)
        """
        lev = float(self.cfg.risk.max_leverage)
        try:
            self.client.set_leverage(product_id, lev)
            log.info("Leverage set to %gx for product_id=%s.", lev, product_id)
            return
        except Exception as exc:
            set_err = exc

        # Setting failed (e.g. an open position blocks changes). Fall back to
        # verifying the CURRENT leverage already matches — if so, we're safe.
        cur_lev = None
        try:
            cur = self.client.get_leverage(product_id)
            raw = cur.get("leverage") if isinstance(cur, dict) else cur
            cur_lev = float(raw)
        except Exception:
            pass

        if cur_lev is not None and abs(cur_lev - lev) < 1e-9:
            log.warning(
                "Could not set leverage (%s), but the exchange already reports "
                "%gx which matches config — continuing.", set_err, lev,
            )
            return

        log.error(
            "ABORTING: leverage is %s but config requires %gx and the change "
            "failed (%s). Refusing to trade with unverified leverage — the "
            "exchange could liquidate before our stop-loss. Set it to %gx "
            "manually in the Delta UI (or fix the API key's Trading permission), "
            "then restart.",
            f"{cur_lev:g}x" if cur_lev is not None else "unknown",
            lev, set_err, lev,
        )
        raise SystemExit(1) from set_err

    def _available_balance(self) -> float:
        try:
            balances = self.client.get_balances()
            for b in balances:
                # USD/USDT settlement wallet
                if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
                    return float(b.get("available_balance", b.get("balance", 0)))
            if balances:
                return float(balances[0].get("available_balance", 0))
        except Exception as exc:
            log.warning("Could not fetch balance (%s); using config starting_balance.", exc)
        return self.cfg.starting_balance

    def _has_open_position(self) -> bool:
        """True only if THIS strategy's own market has an open position.

        Scoped by product_id (falling back to symbol) so two bots that share one
        Delta account — e.g. v2 on BTCUSD + ut_stc on ETHUSD — don't block each
        other: the BTC bot ignores the ETH bot's position and vice-versa.
        """
        if self.dry_run:
            return False
        try:
            positions = self.client.get_positions()
            my_pid = int(self.product["id"]) if self.product and self.product.get("id") is not None else None
            my_sym = str(self.cfg.market.symbol)
            for p in positions:
                if int(p.get("size", 0) or 0) == 0:
                    continue
                pid = p.get("product_id")
                psym = p.get("product_symbol") or (p.get("product") or {}).get("symbol")
                if my_pid is not None and pid is not None:
                    if int(pid) == my_pid:
                        return True
                elif psym is not None:
                    if str(psym) == my_sym:
                        return True
                else:
                    return True  # unknown shape -> be conservative, treat as ours
        except Exception as exc:
            log.warning("Could not fetch positions (%s).", exc)
        return False

    def _roll_day(self, ts: pd.Timestamp) -> None:
        day = ts.date()
        if self._today != day:
            self._today = day
            self._trades_today = 0
