#!/usr/bin/env python3
"""
Rebuild the tracker leaderboard on a schedule.

Until this existed the board had no clock. `trackers.build()` was only ever
called from inside a dashboard request, behind a twelve hour cache, so the
disclosed books of every manager on the board were only re-read when somebody
happened to open the page after the cache went cold. On a quiet week the
leaderboard the product is built on could be days old, and nobody would be told.

Rebuilding on a timer instead has a second benefit worth as much as the
freshness. A cold build reads sixteen CIKs from the SEC and several hundred
symbols from the price API, which takes minutes. Doing that inside an HTTP
handler meant the first visitor after every expiry paid for it and often timed
out. Now the page always reads a file somebody else already built.

Daily is the right cadence even though 13Fs are quarterly: congressional
periodic transaction reports land continuously, filings arrive on no fixed day
inside the 45 day window, and prices move every session, so the excess returns
change daily even when no new filing has appeared.

  refresh_trackers.py             rebuild if the board is older than --max-age
  refresh_trackers.py --force     rebuild regardless
  refresh_trackers.py --check     say how old the board is and exit
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src import trackers  # noqa: E402

log = logging.getLogger("trackers")


def board_age_hours() -> float | None:
    """Hours since the board on disk was written, or None if there is none."""
    path = trackers.CACHE_DIR / "board.json"
    if not path.exists():
        return None
    age = dt.datetime.now().timestamp() - path.stat().st_mtime
    return age / 3600


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the board is fresh")
    ap.add_argument("--max-age", type=float, default=20.0,
                    help="hours before a rebuild is due (default 20)")
    ap.add_argument("--check", action="store_true",
                    help="report the board's age and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s trackers  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    age = board_age_hours()
    if args.check:
        print("no board on disk" if age is None else f"board is {age:.1f}h old")
        return 0

    if age is not None and age < args.max_age and not args.force:
        log.info("board is %.1fh old, under the %.1fh threshold, nothing to do",
                 age, args.max_age)
        return 0

    log.info("rebuilding the board (%s)",
             "no board on disk" if age is None else f"{age:.1f}h old")
    # ttl=0 forces a rebuild rather than reading back the file we are replacing.
    board = trackers.build(ttl=0)

    summary = board.get("summary", {})
    measured = summary.get("measured", 0)
    unmeasured = len(board.get("trackers", [])) - measured
    log.info("%s of %s trackers beat %s, median %s, %s windows dropped for "
             "thin coverage, %s trackers left unmeasured",
             summary.get("beating_benchmark"), measured, board.get("benchmark"),
             summary.get("median_excess"), summary.get("dropped_windows"),
             unmeasured)

    # A board where nothing could be measured is a broken data source, not a
    # market in which every manager happened to fail. It should exit non-zero
    # so the timer's failure count is the thing that gets noticed.
    if not measured:
        log.error("no tracker could be measured at all. This is a data source "
                  "failure, not a result. Check SEC_CONTACT_EMAIL and the "
                  "price API before trusting anything on the board.")
        return 1

    for t in board.get("trackers", []):
        if t.get("error"):
            log.info("  unmeasured: %-32s %s", t["name"][:32], t["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
