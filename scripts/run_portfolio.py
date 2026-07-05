"""Run BOTH portfolio strategies with ONE command.

  py scripts/run_portfolio.py            # dry-run both (no real orders)  [default]
  py scripts/run_portfolio.py --live      # REAL orders on both (one confirmation)
  py scripts/run_portfolio.py --once       # a single evaluation tick each, then exit

Portfolio
---------
  * v2_dualtrend  on BTCUSD 1h   (config.v2_btc.yaml)     — 50% of wallet, 6% risk
  * ut_stc        on ETHUSD 4h   (config.ut_stc_eth.yaml) — 50% of wallet, 5% risk

Each strategy runs as its OWN isolated subprocess, so a crash in one never stops
the other. Their output is streamed to this console, prefixed with the strategy
tag. Press Ctrl+C once to stop both cleanly.

Going live requires TWO things (belt-and-braces):
  1. `--live` here (asks for one typed confirmation), AND
  2. `live.dry_run: false` in BOTH config files.
Until both are set, every order is dry-run (logged, never sent).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_LIVE = ROOT / "scripts" / "run_live.py"

LEGS = [
    ("v2/BTC", ROOT / "config.v2_btc.yaml"),
    ("ut_stc/ETH", ROOT / "config.ut_stc_eth.yaml"),
]


def _pump(proc: subprocess.Popen, tag: str) -> None:
    """Forward a child's output to our stdout, prefixed with its strategy tag."""
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        if line:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run both portfolio strategies together.")
    ap.add_argument("--live", action="store_true",
                    help="Place REAL orders on both legs (otherwise dry-run).")
    ap.add_argument("--once", action="store_true",
                    help="Run a single evaluation tick per leg, then exit.")
    args = ap.parse_args()

    extra: list[str] = []
    if args.once:
        extra.append("--once")
    if args.live:
        print("*** LIVE PORTFOLIO — v2/BTC + ut_stc/ETH ***")
        print("Both config files must ALSO have live.dry_run: false to place real orders.")
        if input("Type 'I UNDERSTAND' to run BOTH legs live: ").strip() != "I UNDERSTAND":
            print("Confirmation not given. Exiting.")
            return
        extra += ["--live", "--yes"]

    procs: list[tuple[str, subprocess.Popen]] = []
    for tag, cfg in LEGS:
        if not cfg.exists():
            print(f"ERROR: config not found: {cfg}")
            _shutdown(procs)
            return
        proc = subprocess.Popen(
            [sys.executable, "-u", str(RUN_LIVE), "--config", str(cfg), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT),
        )
        procs.append((tag, proc))
        threading.Thread(target=_pump, args=(proc, tag), daemon=True).start()
        print(f"started  {tag:<12} pid={proc.pid}  ({cfg.name})")

    print("\nBoth strategies running. Ctrl+C to stop both.\n" + "-" * 60)
    try:
        while any(p.poll() is None for _, p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping both strategies...")
    finally:
        _shutdown(procs)
    print("Portfolio stopped.")


def _shutdown(procs: list[tuple[str, subprocess.Popen]]) -> None:
    for _, p in procs:
        if p.poll() is None:
            p.terminate()
    for _, p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main()
