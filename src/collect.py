"""
Snapshot builder.

Freezes everything the run is allowed to reason about into one dated file
before any analysis happens. If tomorrow's output looks strange you can replay
the exact same snapshot and see whether the data changed or the reasoning did.

Symbols are grouped by broker first, so a portfolio spread across Alpaca, Dhan
and Hyperliquid costs one round of calls per venue rather than one per symbol.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from . import config
from .brokers.base import Broker, BrokerError
from .brokers.registry import Registry

log = logging.getLogger(__name__)

BAR_HISTORY_DAYS = 420  # calendar days, so a 252 trading day lookback fits
#
# The buy and hold rubric looks back a full year (252 trading days) and uses a
# 200 day moving average, so it needs more than a calendar year of bars. Ask
# for 420 calendar days: roughly 290 trading days, with room for holidays.


def snapshot_id(today: dt.date | None = None) -> str:
    return (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()


def build(registry: Registry | None = None, *, days: int = BAR_HISTORY_DAYS) -> dict[str, Any]:
    reg = registry or Registry()
    symbols = config.all_symbols()
    universe = config.universe()

    bars: dict[str, list[dict[str, Any]]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for broker_name, (broker, syms) in reg.by_broker(symbols).items():
        try:
            raw_bars = broker.get_bars(syms, days)
            for sym, series in raw_bars.items():
                bars[sym.upper()] = [
                    {
                        "t": b.ts.isoformat(), "open": b.open, "high": b.high,
                        "low": b.low, "close": b.close, "volume": b.volume,
                    }
                    for b in series
                ]
        except BrokerError as e:
            errors.append(f"{broker_name} bars: {e}")
            log.warning("bars failed on %s: %s", broker_name, e)

        try:
            for sym, q in broker.get_quotes(syms).items():
                quotes[sym.upper()] = {
                    "price": q.price, "t": q.ts.isoformat(),
                    "stale": q.stale, "age_hours": round(q.age_hours, 2),
                }
        except BrokerError as e:
            errors.append(f"{broker_name} quotes: {e}")
            log.warning("quotes failed on %s: %s", broker_name, e)

    # Account and positions come from whichever brokers are actually routed.
    accounts: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
    for name, broker in reg.active().items():
        try:
            accounts[name] = broker.get_account().to_dict()
            for p in broker.get_positions():
                d = p.to_dict()
                d["universe_class"] = config.class_of(p.symbol)
                positions.append(d)
        except BrokerError as e:
            errors.append(f"{name} account: {e}")
            log.warning("account failed on %s: %s", name, e)

    equity = sum(a["equity"] for a in accounts.values()) or 0.0
    cash = sum(a["cash"] for a in accounts.values()) or 0.0
    for p in positions:
        p["weight"] = round(p["market_value"] / equity, 6) if equity else 0.0

    missing = [s for s in symbols if s not in bars or not bars[s]]
    coverage = 1.0 - (len(missing) / len(symbols)) if symbols else 0.0

    return {
        "snapshot_id": snapshot_id(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "config_versions": {
            "universe": universe.get("version"),
            "risk": config.risk().get("version"),
            "scoring": config.scoring().get("version"),
            "brokers": config.brokers().get("version"),
        },
        "routing": {k: reg.for_class(k).name for k in universe["classes"]},
        "broker_health": [
            {"name": h.name, "ok": h.ok, "mode": h.mode, "detail": h.detail}
            for h in reg.health()
        ],
        "degraded": reg.degraded(),
        "broker_failures": reg.failures,
        "benchmark": universe.get("benchmark"),
        "portfolio": {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "cash_weight": round(cash / equity, 6) if equity else 0.0,
            "positions": positions,
            "accounts": accounts,
        },
        "symbols": symbols,
        "bars": bars,
        "quotes": quotes,
        "coverage": round(coverage, 4),
        "missing_data": missing,
        "errors": errors,
    }


def bars_as_objects(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """score.extract_features accepts dicts with a `close`/`volume` key."""
    return snapshot.get("bars", {})
