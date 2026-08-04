"""One-off: replace the hardcoded 0.001 BTC lot size with self.lot_size.

Every one of these is real-money arithmetic — position sizing, cost, proceeds and
P&L — so they are replaced by exact string match and the count is printed for
each, rather than by a regex that could silently miss or over-match.
"""
from __future__ import annotations

import re
from pathlib import Path

SUBS = [
    ("pnl = ((prem - t.entry_premium) * t.lots * 0.001) if prem else 0.0",
     "pnl = ((prem - t.entry_premium) * t.lots * self.lot_size) if prem else 0.0"),
    ("lots = int(stake / (ask * 0.001))",
     "lots = int(stake / (ask * self.lot_size))"),
    ("cost = lots * ask * 0.001",
     "cost = lots * ask * self.lot_size"),
    ("proceeds = lots * max(fair - edge, 0.0) * 0.001",
     "proceeds = lots * max(fair - edge, 0.0) * self.lot_size"),
    ('prem_per_lot = pick["ask"] * 0.001',
     'prem_per_lot = pick["ask"] * self.lot_size'),
    ("pnl = (prem - t.entry_premium) * t.lots * 0.001",
     "pnl = (prem - t.entry_premium) * t.lots * self.lot_size"),
]


def main() -> int:
    p = Path(__file__).resolve().parent.parent / "src" / "live" / "options_trader.py"
    s = p.read_text(encoding="utf-8")
    total = 0
    for old, new in SUBS:
        n = s.count(old)
        total += n
        print(f"  {n}x  {old[:58]}")
        if n:
            s = s.replace(old, new)
    p.write_text(s, encoding="utf-8")
    left = re.findall(r"\* 0\.001", s)
    print(f"\n  {total} replacements written.")
    print(f"  remaining bare '* 0.001' occurrences: {len(left)}")
    for m in re.finditer(r".*\* 0\.001.*", s):
        print(f"    {m.group(0).strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
