"""Run EVERYTHING with one command — both strategies AND the dashboard.

  py run_all.py            # dry-run both strategies + dashboard
  py run_all.py --live      # REAL orders on both + dashboard (one confirmation)

Starts three processes:
  * v2_dualtrend on BTCUSD 1h   (config.v2_btc.yaml)
  * ut_stc       on ETHUSD 4h   (config.ut_stc_eth.yaml)
  * the Streamlit dashboard      -> http://localhost:8501  (auto-opens browser)

Strategy output is streamed here, prefixed [v2/BTC] / [ut_stc/ETH]. The dashboard
is read-only (never places orders). Press Ctrl+C once to stop all three.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_LIVE = ROOT / "scripts" / "run_live.py"

LEGS = [
    ("v2/BTC", ROOT / "config.v2_btc.yaml"),
    ("ut_stc/ETH", ROOT / "config.ut_stc_eth.yaml"),
]


def _pump(proc: subprocess.Popen, tag: str) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        if line:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run both strategies and the dashboard together.")
    ap.add_argument("--live", action="store_true",
                    help="Place REAL orders on both legs (otherwise dry-run).")
    args = ap.parse_args()

    extra: list[str] = []
    if args.live:
        print("*** LIVE — v2/BTC + ut_stc/ETH + dashboard ***")
        print("Both config files must also have live.dry_run: false to place real orders.")
        if input("Type 'I UNDERSTAND' to run BOTH legs live: ").strip() != "I UNDERSTAND":
            print("Confirmation not given. Exiting.")
            return
        extra = ["--live", "--yes"]

    procs: list[tuple[str, subprocess.Popen]] = []

    # --- strategy legs (captured + prefixed) ---
    for tag, cfg in LEGS:
        if not cfg.exists():
            print(f"ERROR: config not found: {cfg}")
            _shutdown(procs)
            return
        p = subprocess.Popen(
            [sys.executable, "-u", str(RUN_LIVE), "--config", str(cfg), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(ROOT),
        )
        procs.append((tag, p))
        threading.Thread(target=_pump, args=(p, tag), daemon=True).start()
        print(f"started  {tag:<12} pid={p.pid}")

    # --- dashboard (inherits console so it prints its URL + opens the browser) ---
    if importlib.util.find_spec("streamlit") is not None:
        d = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard.py")],
            cwd=str(ROOT),
        )
        procs.append(("dashboard", d))
        print("started  dashboard    -> http://localhost:8501")
    else:
        print("dashboard SKIPPED — run  py -m pip install streamlit  once to enable it.")

    print("\nEverything running. Ctrl+C to stop all.\n" + "-" * 60)
    try:
        while any(p.poll() is None for _, p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping everything...")
    finally:
        _shutdown(procs)
    print("Stopped.")


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
