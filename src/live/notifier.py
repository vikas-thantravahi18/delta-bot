"""Telegram trade notifier with 'anime release' code-speak.

On every executed trade the bot sends a DISGUISED Telegram message so a glance at
your phone (or someone reading over your shoulder) never reveals that you trade or
what you did. To you it decodes cleanly; to everyone else it's just an anime alert.

DECODER KEY  (keep this private):
    Which anime  ->  which strategy fired
        One Piece        = ut_stc on BTC OPTIONS  (4h, +600 pts)   <- LIVE
        Jujutsu Kaisen   = ut_stc on ETH OPTIONS  (4h, +30 pts)    <- LIVE
        Demon Slayer     = options_dip   (BTC options 5m)          -- disabled
        Naruto           = ema_rsi_atr   (BTCUSD perp 30m)         -- disabled
        Bleach           = any other strategy

    NOTE One Piece is shared with v2_dualtrend (BTCUSD perp 1h), which is
    currently disabled. If you ever re-enable v2 alongside the BTC options leg,
    both will alert as One Piece — give one of them a different name in ANIME
    below before doing so.

  OPENING a trade
    "new Episode ... dropped"   = a LONG was opened   (a CALL, for options)
    "new Movie announced"       = a SHORT was opened  (a PUT, for options)
    the Episode / Teaser number = the number of lots (position size)

  CLOSING a trade   (options only — the perp legs closed via exchange brackets)
    "finale aired ... rated X" = closed in PROFIT, X = dollars made
    "delayed ... X episodes"   = closed at a LOSS,  X = dollars lost
    "on hiatus"                = closed flat (inside a cent)

    e.g.  "Naruto - Episode 6 just dropped!"  = ema_rsi opened a 6-lot LONG on BTC
          "One Piece - new Movie announced! Teaser 3"  = v2 opened a 3-lot SHORT on BTC
          "Demon Slayer - Episode 104 just dropped!"   = options bought 104 CALL lots
          "Demon Slayer - finale aired ... rated 19.6" = that trade closed +$19.60
          "Demon Slayer - delayed ... 14.2 episodes"   = that trade closed -$14.20

ONE-TIME SETUP:
    1. In Telegram, message @BotFather -> /newbot -> copy the bot TOKEN.
    2. Send your new bot any message, then open in a browser:
         https://api.telegram.org/bot<TOKEN>/getUpdates
       and copy the "chat":{"id": ... } number (your CHAT ID).
    3. Put both in the bot's .env file:
         TELEGRAM_BOT_TOKEN=123456:ABC-your-token
         TELEGRAM_CHAT_ID=123456789
    4. In the strategy's config .yaml set:  notify:\n  enabled: true
    5. Test it:  py scripts/test_notify.py
"""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request

log = logging.getLogger("live")

# strategy name -> cover anime. Add your own; unknown strategies use DEFAULT_ANIME.
#
# The key is what the leg reports as its name, which for the options legs is
# strategy.params.notify_as, NOT the strategy class. Both ut_stc option legs are
# the same strategy on different chains, so without separate keys BTC and ETH
# would send byte-identical alerts and you could not tell which fired.
ANIME = {
    "options_dip": "Demon Slayer",
    "v2_dualtrend": "One Piece",
    "ut_stc_btc": "One Piece",       # ut_stc on the BTC option chain
    "ema_rsi_atr": "Naruto",
    "ut_stc": "Jujutsu Kaisen",      # ut_stc on the ETH option chain
}
DEFAULT_ANIME = "Bleach"


def encode_trade(strategy_name: str, side: str, lots: int) -> str:
    """Turn an OPENED trade into an innocuous 'anime release' message."""
    anime = ANIME.get(strategy_name, DEFAULT_ANIME)
    n = int(lots)
    if str(side).lower() == "long":
        return f"\U0001F37F {anime} — Episode {n} just dropped! Subbed and ready to watch tonight \U0001F525"
    return f"\U0001F3AC {anime} — a new Movie was announced! Teaser {n} is out now \U0001F3A5"


def encode_close(strategy_name: str, pnl: float, reason: str = "") -> str:
    """Turn a CLOSED trade into the same code-speak. The number is the P&L in
    dollars: a 'rating' when we made money, 'delayed episodes' when we lost."""
    anime = ANIME.get(strategy_name, DEFAULT_ANIME)
    amt = abs(float(pnl))
    tail = " (early screening)" if "TARGET" in str(reason).upper() else ""
    if pnl > 0.005:
        return (f"⭐ {anime} — the finale aired{tail}! Fans are rating it "
                f"{amt:.1f} \U0001F3AF")
    if pnl < -0.005:
        return (f"\U0001F4C6 {anime} — the finale got delayed, {amt:.1f} episodes "
                f"pushed back \U0001F614")
    return f"\U0001F4FA {anime} — the series is on hiatus for now ⏸"


class TelegramNotifier:
    """Fire-and-forget Telegram sender. NEVER raises into the trading loop."""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = False) -> None:
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.enabled = bool(enabled and self.token and self.chat_id)
        if enabled and not (self.token and self.chat_id):
            log.warning(
                "notify.enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are "
                "missing from .env - Telegram notifications are OFF."
            )
        elif self.enabled:
            log.info("Telegram notifications ON (anime code-speak).")

    def send(self, text: str) -> bool:
        """Send a raw message. Returns True on success; logs and swallows any error."""
        if not self.enabled:
            return False
        try:
            data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
            req = urllib.request.Request(
                self.API.format(token=self.token), data=data,
                headers={"User-Agent": "delta-bot"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True
        except Exception as exc:  # network hiccup / bad token -> never block trading
            log.warning("Telegram notify failed (%s) - trade still executed.", exc)
            return False

    def notify_trade(self, strategy_name: str, side: str, lots: int) -> None:
        """Send the coded 'anime release' alert for an OPENED trade."""
        self.send(encode_trade(strategy_name, side, lots))

    def notify_close(self, strategy_name: str, pnl: float, reason: str = "") -> None:
        """Send the coded alert for a CLOSED trade, with P&L hidden in the number."""
        self.send(encode_close(strategy_name, pnl, reason))
