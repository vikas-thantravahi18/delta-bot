"""Send a test Telegram notification to verify the setup.

  py scripts/test_notify.py                 # uses config.yaml (or the example)
  py scripts/test_notify.py config.v2_btc.yaml

Sends one sample coded alert for each strategy so you can confirm the token/chat id
work and see what the 'anime release' messages look like. Requires TELEGRAM_BOT_TOKEN
and TELEGRAM_CHAT_ID in .env and notify.enabled: true in the config.
"""
import sys
from pathlib import Path

try:                                  # let the Windows console print emoji
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.live.notifier import TelegramNotifier, encode_trade

cfg = Config.load(sys.argv[1] if len(sys.argv) > 1 else None)
n = TelegramNotifier(cfg.notify.telegram_token, cfg.notify.telegram_chat_id, cfg.notify.enabled)

if not n.enabled:
    print("Notifications are OFF. Check: notify.enabled: true in the yaml, and "
          "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set in .env.")
    raise SystemExit(1)

samples = [("v2_dualtrend", "long", 5), ("ema_rsi_atr", "short", 3), ("ut_stc", "long", 2)]
print("Sending sample coded alerts to Telegram...")
for name, side, lots in samples:
    text = encode_trade(name, side, lots)
    ok = n.send(text)
    print(f"  [{'sent' if ok else 'FAIL'}] {name} {side} {lots} lots -> {text}")
print("Done. If you saw them in Telegram, the wiring works. (Decoder key: live/notifier.py)")
