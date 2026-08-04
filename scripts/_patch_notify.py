"""One-off: stop the options trader labelling every alert as "options_dip".

Three call sites hardcoded the BTC strategy name, so the ETH leg would have sent
its alerts under Demon Slayer — indistinguishable from the BTC bot. Replaced with
self.notify_name, which defaults to the configured strategy name.
"""
from __future__ import annotations

from pathlib import Path

SUBS = [
    ('self.notifier.notify_trade("options_dip", sig.side, lots)',
     'self.notifier.notify_trade(self.notify_name, sig.side, lots)'),
    ('self.notifier.notify_close("options_dip", pnl, why)',
     'self.notifier.notify_close(self.notify_name, pnl, why)'),
]


def main() -> int:
    p = Path(__file__).resolve().parent.parent / "src" / "live" / "options_trader.py"
    s = p.read_text(encoding="utf-8")
    total = 0
    for old, new in SUBS:
        n = s.count(old)
        total += n
        print(f"  {n}x  {old}")
        s = s.replace(old, new)
    p.write_text(s, encoding="utf-8")
    left = s.count('"options_dip"')
    print(f"\n  {total} replaced. Remaining literal \"options_dip\": {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
