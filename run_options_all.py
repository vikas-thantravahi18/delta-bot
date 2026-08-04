"""Run BOTH options legs plus the dashboard with one command.

  py run_options_all.py                  # dry-run both + dashboard
  py run_options_all.py --live           # REAL orders, both legs
  py run_options_all.py --live --only eth   # just the ETH leg
  py run_options_all.py --live --reset   # force the return counter back to zero

Starts up to three processes:
  * options_dip — BTC options, 5m signal, +400 pt target, 10% of wallet
  * ut_stc      — ETH options, 4h signal, +30 pt target, 10% of wallet
  * the Streamlit dashboard -> http://localhost:8501

BOTH LEGS SHARE ONE WALLET. Each sizes at 10% of the balance it reads at the
moment it fires, so whichever trades first shrinks the stake available to the
other. That is deliberate — it self-limits total exposure — but it means the two
are not independent, and a big BTC position makes the next ETH trade smaller.

THE DASHBOARD RESET IS AUTOMATIC AND ONE-TIME.
data/dashboard_baseline.json fixes the capital and start time that "total return"
is measured against. This script creates it on first run and then LEAVES IT ALONE,
so restarting the bot never wipes your track record. Pass --reset when you
deliberately want a clean slate (new capital, a long break, a config change).

Bot output is streamed here prefixed [options]. The dashboard is read-only and
never places orders. Ctrl+C once stops both.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_OPTIONS = ROOT / "scripts" / "run_options.py"
CONFIG = ROOT / "config.options_btc.yaml"
ETH_CONFIG = ROOT / "config.ut_stc_eth_options.yaml"
BASELINE = ROOT / "data" / "dashboard_baseline.json"


def _pump(proc: subprocess.Popen, tag: str) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        if line:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()


def ensure_baseline(force: bool) -> None:
    """Create the dashboard baseline once; only rewrite it when asked."""
    if BASELINE.exists() and not force:
        try:
            d = json.loads(BASELINE.read_text())
            print(f"dashboard counter: running since {str(d['strategy_start'])[:19]} "
                  f"on ${float(d['starting_capital']):,.2f} (kept — use --reset to restart)")
            return
        except Exception:
            print("dashboard baseline unreadable — recreating.")
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "reset_dashboard.py"), "--yes"]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    tail = [ln for ln in (r.stdout or "").splitlines() if "new baseline" in ln]
    print(f"dashboard counter: RESET — {tail[0].strip() if tail else 'baseline written'}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the BTC options bot and the dashboard together.")
    ap.add_argument("--live", action="store_true",
                    help="Place REAL orders (otherwise dry-run).")
    ap.add_argument("--reset", action="store_true",
                    help="Reset the dashboard return counter to now + current balance.")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--only", choices=["btc", "eth"],
                    help="Run just one leg instead of both.")
    args = ap.parse_args()

    legs = [("btc", CONFIG), ("eth", ETH_CONFIG)]
    if args.only:
        legs = [(n, c) for n, c in legs if n == args.only]
    legs = [(n, c) for n, c in legs if c.exists()]
    if not legs:
        print("ERROR: no config found for the requested legs.")
        return

    extra: list[str] = []
    if args.live:
        print("\n*** LIVE — OPTIONS + dashboard ***")
        for n, c in legs:
            print(f"  [{n}] {c.name}")
        print("\n  BTC  options_dip · 5m · +400 pts · 10% · no cooldown, no IV ceiling")
        print("  ETH  ut_stc · 4h · +30 pts · 10% · ~11 trades/month")
        print("  Both: no stop-loss — the option premium is the maximum loss.")
        print("  Both legs size off the SAME wallet, so they compete for balance.")
        print("  Each config must also have live.dry_run: false")
        if input("\nType 'I UNDERSTAND' to trade real money: ").strip() != "I UNDERSTAND":
            print("Confirmation not given. Exiting.")
            return
        extra = ["--live", "--yes"]
    else:
        print("\nDRY-RUN — no real orders. Add --live to trade.")
        for n, c in legs:
            print(f"  [{n}] {c.name}")
        print()

    ensure_baseline(force=args.reset)

    procs: list[tuple[str, subprocess.Popen]] = []

    for name, conf in legs:
        p = subprocess.Popen(
            [sys.executable, "-u", str(RUN_OPTIONS), "--config", str(conf), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(ROOT),
        )
        procs.append((name, p))
        threading.Thread(target=_pump, args=(p, name), daemon=True).start()
        print(f"started  {name:<12} pid={p.pid}  ({conf.name})")

    if importlib.util.find_spec("streamlit") is not None:
        d = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard.py"),
             "--server.headless", "true", "--server.address", "0.0.0.0",
             "--server.port", str(args.port)],
            cwd=str(ROOT),
        )
        procs.append(("dashboard", d))
        print(f"started  dashboard    -> http://localhost:{args.port}")
    else:
        print("dashboard SKIPPED — run  py -m pip install streamlit  once to enable it.")

    print("\nRunning. Ctrl+C to stop both.\n" + "-" * 60)
    try:
        while any(pr.poll() is None for _, pr in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        _shutdown(procs)
    print("Stopped.")
    if args.live:
        print("NOTE: if a position was open, the bot is no longer managing it — "
              "check the Delta UI and close it manually.")


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
