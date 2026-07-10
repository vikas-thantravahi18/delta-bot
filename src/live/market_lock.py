"""Cross-process, per-market entry guard for the live bot.

Two bot processes can trade the SAME Delta market on ONE account — e.g. the
``v2`` and ``ema_rsi`` legs both run on BTCUSD. A single account holds ONE net
position per market, and each process does:

    check exchange for an open position  ->  (if flat) place an order

That sequence is NOT atomic across processes. In the small window after process
A checks "flat" but before its order fills, process B can also check "flat" and
enter too — a double entry with two brackets and double the intended risk.

This module closes that window on a single host:

* ``MarketLock(symbol)`` — an exclusive lock file so only ONE process at a time
  may run the check-and-enter section for a given market. Keyed by symbol, so
  BTCUSD and ETHUSD legs never block each other. A lock older than ``stale``
  seconds is assumed abandoned (crashed holder) and stolen, so a crash can never
  wedge a market permanently.
* ``write_claim`` / ``has_fresh_claim`` — a short-lived marker written right after
  an entry. It bridges the lag between placing an order and the exchange
  reporting the resulting position (during which ``get_positions()`` may still
  say "flat"), so the other leg treats the market as occupied in the meantime.

File-based => all legs sharing a market MUST run on the same machine (they do:
run_all.py launches them as sibling subprocesses).
"""
from __future__ import annotations

import errno
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("live")

# repo_root/data/locks  (market_lock.py is at src/live/)
LOCK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "locks"


def _safe(symbol: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in symbol)


class MarketLock:
    """Exclusive per-market lock across processes on one host.

    Use as a context manager; the bound value is True if the lock was acquired
    and False if another process already holds it (caller should then skip):

        with MarketLock("BTCUSD") as locked:
            if not locked:
                return
            ... check position, size, place order ...
    """

    def __init__(self, symbol: str, wait: float = 8.0, stale: float = 30.0) -> None:
        self.symbol = symbol
        self.path = LOCK_DIR / f"{_safe(symbol)}.lock"
        self.wait = wait
        self.stale = stale
        self.fd: int | None = None

    def __enter__(self) -> bool:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait
        while True:
            try:
                # Atomic: only ONE process can create the file exclusively.
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return True
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                # Held by someone else — steal it only if it's stale (crashed holder).
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale:
                        log.warning("Stealing stale %s lock (age %.0fs).", self.symbol, age)
                        os.unlink(self.path)
                        continue
                except FileNotFoundError:
                    continue  # released just now — retry immediately
                if time.time() >= deadline:
                    return False  # give up; the holder is entering — caller skips
                time.sleep(0.2)

    def __exit__(self, *exc) -> None:
        # Only release if WE actually acquired the lock. A process that failed to
        # acquire (fd is None) must never delete the current holder's lock file.
        if self.fd is None:
            return
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass
        self.fd = None


def _claim_path(symbol: str) -> Path:
    return LOCK_DIR / f"{_safe(symbol)}.claim"


def write_claim(symbol: str) -> None:
    """Mark ``symbol`` as just-entered (call right after placing a real order)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _claim_path(symbol).write_text(str(time.time()))
    except OSError as exc:
        log.warning("Could not write entry claim for %s (%s).", symbol, exc)


def has_fresh_claim(symbol: str, ttl: float = 120.0) -> bool:
    """True if some process entered ``symbol`` within the last ``ttl`` seconds.

    Bridges the exchange's position-propagation lag so a second leg does not
    double-enter in the seconds between an order filling and the position showing
    up in get_positions().
    """
    try:
        age = time.time() - _claim_path(symbol).stat().st_mtime
    except FileNotFoundError:
        return False
    return age < ttl
