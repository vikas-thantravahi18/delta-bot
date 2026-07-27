"""Telegram trade notifier with 'anime release' code-speak.

On every executed trade the bot sends a DISGUISED Telegram message so a glance at
your phone (or someone reading over your shoulder) never reveals that you trade or
what you did. To you it decodes cleanly; to everyone else it's just an anime alert.

DECODER KEY  (keep this private):
    Which anime  ->  which strategy fired
        One Piece        = v2_dualtrend   (BTCUSD 1h)
        Naruto           = ema_rsi_atr    (BTCUSD 30m)
        Jujutsu Kaisen   = ut_stc         (ETHUSD 4h)
        Bleach           = any other strategy
    "new Episode ... dropped"   = a LONG (buy) was opened
    "new Movie announced"       = a SHORT (sell) was opened
    the Episode / Teaser number = the number of lots (position size)

    e.g.  "Naruto - Episode 6 just dropped!"  = ema_rsi opened a 6-lot LONG on BTC
          "One Piece - new Movie announced! Teaser 3"  = v2 opened a 3-lot SHORT on BTC

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
ANIME = {
    "v2_dualtrend": "One Piece",
    "ema_rsi_atr": "Naruto",
    "ut_stc": "Jujutsu Kaisen",
}
DEFAULT_ANIME = "Bleach"


def encode_trade(strategy_name: str, side: str, lots: int) -> str:
    """Turn a trade into an innocuous 'anime release' message (see DECODER KEY)."""
    anime = ANIME.get(strategy_name, DEFAULT_ANIME)
    n = int(lots)
    if str(side).lower() == "long":
        return f"\U0001F37F {anime} — Episode {n} just dropped! Subbed and ready to watch tonight \U0001F525"
    return f"\U0001F3AC {anime} — a new Movie was announced! Teaser {n} is out now \U0001F3A5"


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
        """Send the coded 'anime release' alert for an executed trade."""
        self.send(encode_trade(strategy_name, side, lots))
