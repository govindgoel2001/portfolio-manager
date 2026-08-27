#!/usr/bin/env python3
"""
Sit the committee on the next few names.

Runs eight times a day on a rotation rather than all at once on everything.
A sitting costs four models several minutes of real inference, so covering
thirty one instruments in one pass would take hours and would produce a wall
of verdicts nobody reads. Three at a time, eight times a day, covers the whole
universe roughly every eighteen hours and keeps every verdict recent.

The rotation is a cursor on disk, not a random sample. Random sampling leaves
gaps: over a week some names get picked four times and others never, and the
ones that never come up are exactly the ones nobody is thinking about.

What comes out of a sitting is a verdict and a multiplier between 0.8 and 1.2.
The multiplier is read by the next allocation run and applied to a weight the
deterministic allocator already chose. The committee never picks a name, never
sets a size, and never bypasses the risk gate.

  run_committee.py                  the next three on the rotation
  run_committee.py --symbols X,Y    specific names, cursor untouched
  run_committee.py --count 5        a longer sitting
  run_committee.py --sectors        rotate sector funds instead of companies
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src import committee, config, ingest, llm, rag, state  # noqa: E402
from src.brokers.registry import Registry  # noqa: E402
from src.brokers.base import BrokerError  # noqa: E402
from src.data import fundamentals as fu  # noqa: E402
from src.data import sectors as sectors_mod  # noqa: E402
from src.data import smartmoney as sm  # noqa: E402

log = logging.getLogger("committee")
CURSOR = config.DATA_DIR / "committee_cursor.json"


def _rotation(sectors_only: bool) -> list[str]:
    if sectors_only:
        return list(sectors_mod.SECTORS)
    # Companies first, sector funds last, so a cold start spends its early
    # sittings on the names that carry position-level risk.
    every = [s for s in config.active_symbols() if "/" not in s]
    funds = [s for s in every if s in sectors_mod.SECTORS]
    return [s for s in every if s not in funds] + funds


def _next(count: int, sectors_only: bool) -> list[str]:
    order = _rotation(sectors_only)
    if not order:
        return []
    try:
        cursor = json.loads(CURSOR.read_text(encoding="utf-8")).get("index", 0)
    except (OSError, json.JSONDecodeError):
        cursor = 0

    picked = [order[(cursor + i) % len(order)] for i in range(min(count, len(order)))]
    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    CURSOR.write_text(json.dumps({
        "index": (cursor + len(picked)) % len(order),
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "last": picked,
    }), encoding="utf-8")
    return picked


def _evidence(symbol: str, reg: Registry, signals: dict) -> dict:
    quotes = {}
    try:
        for _n, (broker, syms) in reg.by_broker([symbol]).items():
            quotes.update(broker.get_quotes(syms))
    except BrokerError as e:
        log.warning("no quote for %s: %s", symbol, e)

    q = quotes.get(symbol)
    sector_context = None
    if symbol in sectors_mod.SECTORS:
        try:
            sector_context = sectors_mod.detail(symbol, signals=signals)
        except Exception as e:  # noqa: BLE001
            log.warning("sector context for %s failed: %s", symbol, e)

    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "instrument": "sector fund" if sector_context else "company",
        "sector_context": sector_context,
        "quote": ({"price": q.price, "as_of": q.ts.isoformat(),
                   "stale": q.stale} if q else None),
        "fundamentals": {k: v.to_dict() for k, v in fu.fetch([symbol]).items()},
        "smart_money": (signals[symbol].to_dict() if symbol in signals else None),
        "retrieved_context": rag.context_for(
            symbol, f"{symbol} outlook, risks, valuation and recent developments", k=6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma separated, skips the rotation")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--sectors", action="store_true",
                    help="rotate the eleven sector funds instead")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s committee %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if not llm.available():
        log.error("no reasoning backend, so there is no committee to convene")
        return 1

    universe = {s.upper() for s in config.all_symbols()}
    if args.symbols:
        picked = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        unknown = [s for s in picked if s not in universe]
        if unknown:
            log.error("not in the universe: %s", ", ".join(unknown))
            return 2
    else:
        picked = _next(args.count, args.sectors)

    if not picked:
        log.info("nothing to sit on")
        return 0

    log.info("sitting on %s via %s", ", ".join(picked), llm.backend_name())

    reg = Registry()
    store = state.Store()
    try:
        signals = sm.collect()
    except Exception as e:  # noqa: BLE001
        log.warning("smart money unavailable: %s", e)
        signals = {}

    failures = 0
    for symbol in picked:
        try:
            verdict = committee.convene(symbol, _evidence(symbol, reg, signals))
        except Exception as e:  # noqa: BLE001 - one bad name is not a bad run
            log.error("committee on %s failed: %s", symbol, e)
            failures += 1
            continue

        payload = verdict.to_dict()
        store.save_committee(symbol, payload)
        ingest.committee(symbol, payload)
        log.info("%-6s %-14s %.1f/10  disagreement %.0f  multiplier %.3f  (%d/%d seats)",
                 symbol, verdict.consensus, verdict.score, verdict.disagreement,
                 verdict.multiplier, verdict.answered, verdict.total)

    # A run where every seat failed everywhere is a broken backend, not a
    # quiet day, and it should exit non-zero so the timer reports it.
    if failures == len(picked):
        log.error("every sitting failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
