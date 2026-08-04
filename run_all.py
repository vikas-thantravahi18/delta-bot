"""Run every ENABLED strategy plus the dashboard with one command.

  py run_all.py            # dry-run everything enabled + dashboard
  py run_all.py --live     # REAL orders (one confirmation)
  py run_all.py --live --reset    # also re-base the dashboard return counter

WHICH LEGS RUN IS DECIDED BY THE CONFIG FILES, NOT BY THIS SCRIPT.
A leg is started when its config has `live.dry_run: false`. That way enabling or
disabling a strategy is a one-line edit in one place, and this launcher can never
disagree with what the bot will actually do.

Each leg is routed to the right runner automatically:
  * options strategies (options_dip, ut_stc on an option chain) -> run_options.py
  * perp strategies    (v2_dualtrend, ema_rsi_atr, ut_stc perp) -> run_live.py

The two BTC perp legs share one net position: a per-market lock
(src/live/market_lock.py) lets whichever fires first take the slot and makes the
other skip, so there is never a double BTC entry.

Output is streamed here prefixed with each leg's tag. Ctrl+C once stops everything.
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

import yaml

ROOT = Path(__file__).resolve().parent
RUN_LIVE = ROOT / "scripts" / "run_live.py"
RUN_OPTIONS = ROOT / "scripts" / "run_options.py"
BASELINE = ROOT / "data" / "dashboard_baseline.json"

# tag, config file, is_options
LEGS = [
    ("btc-opt", "config.options_btc.yaml", True),
    ("eth-opt", "config.ut_stc_eth_options.yaml", True),
    ("v2/BTC", "config.v2_btc.yaml", False),
    ("ema/BTC", "config.ema_rsi_btc.yaml", False),
    ("ut_stc/ETH", "config.ut_stc_eth.yaml", False),
]


def _pump(proc: subprocess.Popen, tag: str) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        if line:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()


def enabled(path: Path) -> bool:
    """A leg is live when its own config says so. Missing file = not enabled."""
    try:
        d = yaml.safe_load(path.read_text()) or {}
        return not bool(((d.get("live") or {}).get("dry_run", True)))
    except Exception:
        return False


def describe(path: Path) -> str:
    try:
        d = yaml.safe_load(path.read_text()) or {}
        s = (d.get("strategy") or {}).get("name", "?")
        m = (d.get("market") or {})
        return f"{s} · {m.get('symbol','?')} {m.get('resolution','?')}"
    except Exception:
        return "?"


def ensure_baseline(force: bool) -> None:
    if BASELINE.exists() and not force:
        try:
            d = json.loads(BASELINE.read_text())
            print(f"dashboard counter: since {str(d['strategy_start'])[:19]} on "
                  f"${float(d['starting_capital']):,.2f} (use --reset to re-base)")
            return
        except Exception:
            print("dashboard baseline unreadable — recreating.")
    r = subprocess.run([sys.executable, "-u",
                        str(ROOT / "scripts" / "reset_dashboard.py"), "--yes"],
                       cwd=str(ROOT), capture_output=True, text=True)
    tail = [ln for ln in (r.stdout or "").splitlines() if "new baseline" in ln]
    print(f"dashboard counter: RESET — {tail[0].strip() if tail else 'written'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run every enabled strategy + dashboard.")
    ap.add_argument("--live", action="store_true", help="Place REAL orders.")
    ap.add_argument("--reset", action="store_true",
                    help="Re-base the dashboard return counter to now.")
    ap.add_argument("--port", type=int, default=8501)
    args = ap.parse_args()

    active = [(tag, ROOT / f, is_opt) for tag, f, is_opt in LEGS
              if (ROOT / f).exists() and enabled(ROOT / f)]
    off = [(tag, ROOT / f) for tag, f, _ in LEGS
           if (ROOT / f).exists() and not enabled(ROOT / f)]

    print("\nENABLED (live.dry_run: false):")
    for tag, path, is_opt in active:
        print(f"  {tag:<12} {describe(path):<34} {'options' if is_opt else 'perp'}")
    if not active:
        print("  none — every config has dry_run: true. Nothing to run.")
        return
    if off:
        print("\ndisabled:")
        for tag, path in off:
            print(f"  {tag:<12} {describe(path)}")

    extra: list[str] = []
    if args.live:
        print("\n*** LIVE — REAL ORDERS ON THE LEGS LISTED ABOVE ***")
        if any(not o for _, _, o in active):
            print("  WARNING: perp legs run in MARGIN mode — 50% of wallet as")
            print("  margin at up to 10x. Their loss is NOT capped like an option's.")
        print("  All legs size off the SAME wallet and compete for balance.")
        if input("\nType 'I UNDERSTAND' to trade real money: ").strip() != "I UNDERSTAND":
            print("Confirmation not given. Exiting.")
            return
        extra = ["--live", "--yes"]
    else:
        print("\nDRY-RUN — no real orders. Add --live to trade.\n")

    ensure_baseline(force=args.reset)
    procs: list[tuple[str, subprocess.Popen]] = []

    for tag, path, is_opt in active:
        runner = RUN_OPTIONS if is_opt else RUN_LIVE
        p = subprocess.Popen(
            [sys.executable, "-u", str(runner), "--config", str(path), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, cwd=str(ROOT),
        )
        procs.append((tag, p))
        threading.Thread(target=_pump, args=(p, tag), daemon=True).start()
        print(f"started  {tag:<12} pid={p.pid}  ({path.name})")

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

    print("\nRunning. Ctrl+C to stop all.\n" + "-" * 60)
    try:
        while any(pr.poll() is None for _, pr in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        _shutdown(procs)
    print("Stopped.")
    if args.live:
        print("NOTE: any open position is no longer being managed — check the "
              "Delta UI and close manually if needed.")


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
