"""
Mock broker - a deterministic paper account with no external dependencies.

Exists so the entire system (pipeline, risk gate, dashboard, approval flow)
is exercisable and testable before any Alpaca key exists, and so tests never
touch the network. Prices are a seeded geometric random walk with a per-symbol
drift and vol, so the same seed always produces the same series.

State persists to data/mock_state.json, which means fills survive a restart
and the dashboard shows a portfolio that actually evolves.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import uuid
from pathlib import Path
from typing import Any, Iterable

from .base import (
    Account, Bar, Broker, BrokerError, BrokerHealth, EquityPoint,
    OrderRequest, OrderResult, Position, Quote,
)

# Rough, hand-set annualised drift/vol per symbol so the mock portfolio behaves
# plausibly by asset class rather than every line moving identically.
PROFILES: dict[str, tuple[float, float, float]] = {
    # symbol: (start_price, annual_drift, annual_vol)
    "AAPL":    (225.0, 0.10, 0.26),
    "MSFT":    (430.0, 0.12, 0.24),
    "NVDA":    (128.0, 0.30, 0.52),
    "AMZN":    (185.0, 0.11, 0.30),
    "GOOGL":   (168.0, 0.09, 0.28),
    "META":    (520.0, 0.14, 0.34),
    "AVGO":    (165.0, 0.18, 0.38),
    "JPM":     (215.0, 0.07, 0.20),
    "UNH":     (300.0, 0.04, 0.25),
    "COST":    (890.0, 0.10, 0.19),
    "GLD":     (255.0, 0.08, 0.14),
    "IAU":     (52.0,  0.08, 0.14),
    "GDX":     (44.0,  0.12, 0.32),
    "USO":     (72.0, -0.02, 0.34),
    "BNO":     (30.0, -0.01, 0.33),
    "XLE":     (88.0,  0.05, 0.24),
    "SPY":     (560.0, 0.09, 0.16),
    "BTC/USD": (62000.0, 0.25, 0.60),
    "ETH/USD": (2600.0,  0.20, 0.70),
    "SOL/USD": (145.0,   0.30, 0.90),
    "LINK/USD": (11.5,   0.15, 0.80),
}
DEFAULT_PROFILE = (100.0, 0.06, 0.30)
STARTING_CASH = 10_000.0


class MockBroker(Broker):
    adapter = "mock"
    supports_notional = True
    supports_fractional = True
    always_open_classes = ("crypto",)

    def __init__(
        self,
        name: str = "mock",
        *,
        seed: int = 20260826,
        supports: tuple[str, ...] = ("us_stocks", "gold", "oil", "crypto",
                                     "sectors"),
        state_path: str | Path = "data/mock_state.json",
        starting_cash: float = STARTING_CASH,
        as_of: dt.datetime | None = None,
    ) -> None:
        self.name = name
        self.mode = "paper"  # type: ignore[assignment]
        self.supports = supports
        self.seed = seed
        self._path = Path(state_path)
        self._starting_cash = starting_cash
        #: pin the clock, so a test can place itself inside a trading session
        #: instead of inheriting whatever day the suite happens to run on
        self._as_of = as_of
        self._state = self._load()

    def _now(self) -> dt.datetime:
        return self._as_of or dt.datetime.now(dt.timezone.utc)

    # -- persisted state --------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except json.JSONDecodeError:
                pass
        return {"cash": self._starting_cash, "positions": {}, "orders": []}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    def reset(self) -> None:
        self._state = {"cash": self._starting_cash, "positions": {}, "orders": []}
        self._save()

    # -- deterministic price series ---------------------------------------

    def _series(self, symbol: str, days: int) -> list[Bar]:
        start, drift, vol = PROFILES.get(symbol.upper(), DEFAULT_PROFILE)
        rng = random.Random(f"{self.seed}:{symbol.upper()}")
        dt_step = 1 / 252
        mu = (drift - 0.5 * vol * vol) * dt_step
        sigma = vol * math.sqrt(dt_step)

        price = start
        bars: list[Bar] = []
        now = self._now()
        session = now.replace(hour=20, minute=0, second=0, microsecond=0)
        is_crypto = "/" in symbol

        # History runs to the session before this one, then the newest bar is
        # stamped at `now`.
        #
        # It used to stop at the previous session and go no further, and weekend
        # dates were skipped for equities, so the freshest price this broker
        # could return was yesterday's close at best and Friday's close all
        # weekend. The freshness gate rejects anything over 36 hours, so a
        # simulated account could not trade at all from Friday evening until
        # Tuesday morning, and the reason it gave was stale data rather than a
        # closed market. A broker asked what the price is right now should
        # answer with a print from right now.
        for i in range(days, 0, -1):
            ts = session - dt.timedelta(days=i)
            if not is_crypto and ts.weekday() >= 5:
                continue
            price *= math.exp(mu + sigma * rng.gauss(0, 1))
            bars.append(self._bar(ts, price, rng, mu, sigma, is_crypto))

        price *= math.exp(mu + sigma * rng.gauss(0, 1))
        bars.append(self._bar(now, price, rng, mu, sigma, is_crypto))
        return bars

    def _bar(self, ts, price, rng, mu, sigma, is_crypto) -> Bar:
        intraday = abs(rng.gauss(0, sigma * 0.6))
        open_ = price * (1 + rng.gauss(0, sigma * 0.3))
        high = max(open_, price) * (1 + intraday)
        low = min(open_, price) * (1 - intraday)
        volume = abs(rng.gauss(1.0, 0.25)) * (2e7 if not is_crypto else 5e5)
        return Bar(ts=ts, open=round(open_, 4), high=round(high, 4),
                   low=round(low, 4), close=round(price, 4),
                   volume=round(volume, 2))

    # -- required interface -----------------------------------------------

    def health(self) -> BrokerHealth:
        eq = self.get_account().equity
        return BrokerHealth(
            self.name, True, "paper",
            f"simulated account seed={self.seed} equity=${eq:,.2f} "
            f"(no Alpaca keys - prices are synthetic)",
            self.supports,
        )

    def get_account(self) -> Account:
        cash = float(self._state["cash"])
        held = self._state["positions"]
        equity = cash
        if held:
            quotes = self.get_quotes(list(held))
            equity += sum(p["qty"] * quotes[s].price for s, p in held.items() if s in quotes)
        return Account(equity=round(equity, 2), cash=round(cash, 2),
                       buying_power=round(cash, 2), broker=self.name, mode="paper")

    def get_positions(self) -> list[Position]:
        held = self._state["positions"]
        if not held:
            return []
        quotes = self.get_quotes(list(held))
        out: list[Position] = []
        for sym, p in held.items():
            price = quotes[sym].price if sym in quotes else p["avg_cost"]
            mv = p["qty"] * price
            cost = p["qty"] * p["avg_cost"]
            out.append(Position(
                symbol=sym, qty=round(p["qty"], 8), avg_cost=round(p["avg_cost"], 4),
                market_value=round(mv, 2), unrealized_pl=round(mv - cost, 2),
                unrealized_plpc=round((mv - cost) / cost, 6) if cost else 0.0,
                asset_class="crypto" if "/" in sym else "equity", broker=self.name,
            ))
        return out

    def get_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]:
        return {s.upper(): self._series(s, max(days, 5)) for s in symbols}

    def get_quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym, bars in self.get_bars(symbols, 30).items():
            if bars:
                out[sym] = Quote(sym, bars[-1].close, bars[-1].ts, stale=False)
        return out

    def submit_order(self, order: OrderRequest) -> OrderResult:
        sym = order.symbol.upper()
        quotes = self.get_quotes([sym])
        if sym not in quotes:
            raise BrokerError(f"{self.name}: no price for {sym}")
        price = quotes[sym].price

        qty = order.qty if order.qty is not None else (order.notional or 0.0) / price
        held = self._state["positions"]
        cash = float(self._state["cash"])

        if order.side == "buy":
            cost = qty * price
            if cost > cash + 1e-6:
                raise BrokerError(
                    f"{self.name}: insufficient cash for {sym} "
                    f"(need ${cost:,.2f}, have ${cash:,.2f})"
                )
            prev = held.get(sym, {"qty": 0.0, "avg_cost": price})
            new_qty = prev["qty"] + qty
            held[sym] = {
                "qty": new_qty,
                "avg_cost": (prev["qty"] * prev["avg_cost"] + cost) / new_qty,
            }
            self._state["cash"] = cash - cost
        else:
            prev = held.get(sym)
            if not prev:
                raise BrokerError(f"{self.name}: no position in {sym} to sell")
            qty = min(qty, prev["qty"])
            self._state["cash"] = cash + qty * price
            remaining = prev["qty"] - qty
            if remaining <= 1e-9:
                held.pop(sym)
            else:
                held[sym] = {"qty": remaining, "avg_cost": prev["avg_cost"]}

        result = OrderResult(
            id=str(uuid.uuid4()), symbol=sym, side=order.side, status="filled",
            submitted_at=dt.datetime.now(dt.timezone.utc),
            filled_qty=round(qty, 8), filled_avg_price=round(price, 4),
            broker=self.name, note="simulated fill at last close",
        )
        self._state["orders"].insert(0, result.to_dict())
        del self._state["orders"][200:]
        self._save()
        return result

    def get_orders(self, status: str = "all", limit: int = 50) -> list[OrderResult]:
        out: list[OrderResult] = []
        for d in self._state["orders"][:limit]:
            out.append(OrderResult(
                id=d["id"], symbol=d["symbol"], side=d["side"], status=d["status"],
                submitted_at=dt.datetime.fromisoformat(d["submitted_at"]),
                filled_qty=d["filled_qty"], filled_avg_price=d["filled_avg_price"],
                broker=self.name, note=d.get("note", ""),
            ))
        return out

    def cancel_order(self, order_id: str) -> bool:
        return False  # mock fills instantly; nothing is ever open to cancel

    def is_market_open(self, asset_class: str = "us_stocks") -> bool:
        if asset_class in self.always_open_classes:
            return True
        now = dt.datetime.now(dt.timezone.utc)
        return now.weekday() < 5 and 13 <= now.hour < 20

    def get_portfolio_history(self, days: int = 90) -> list[EquityPoint]:
        """
        Replay the fill log over the price series to get a real equity curve.

        This is genuine history, not a backward projection of today's book: on
        every day it values only the positions that were actually held then. A
        stretch with no fills is a flat line at the starting cash, which is
        exactly what the account did.
        """
        fills = sorted(
            self._state.get("orders", []),
            key=lambda o: o["submitted_at"],
        )
        traded = {o["symbol"] for o in fills} | set(self._state["positions"])
        series = self.get_bars(traded, days) if traded else {}

        # Trading-day calendar. Crypto has bars every day, so prefer an equity
        # series if there is one, else fall back to whatever we have.
        calendar: list[dt.datetime] = []
        for sym, bars in series.items():
            if "/" not in sym and len(bars) > len(calendar):
                calendar = [b.ts for b in bars]
        if not calendar:
            for bars in series.values():
                if len(bars) > len(calendar):
                    calendar = [b.ts for b in bars]
        if not calendar:
            calendar = [
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=d)
                for d in range(days, -1, -1)
            ]

        closes: dict[str, dict[str, float]] = {
            sym: {b.ts.date().isoformat(): b.close for b in bars}
            for sym, bars in series.items()
        }

        out: list[EquityPoint] = []
        for day in calendar:
            key = day.date().isoformat()
            cash = self._starting_cash
            book: dict[str, float] = {}
            for o in fills:
                if o["submitted_at"][:10] > key:
                    break  # fills are sorted, nothing later can apply
                qty = float(o["filled_qty"])
                price = float(o["filled_avg_price"] or 0.0)
                if o["side"] == "buy":
                    cash -= qty * price
                    book[o["symbol"]] = book.get(o["symbol"], 0.0) + qty
                else:
                    cash += qty * price
                    book[o["symbol"]] = book.get(o["symbol"], 0.0) - qty

            equity = cash
            for sym, qty in book.items():
                if qty <= 1e-9:
                    continue
                price = closes.get(sym, {}).get(key)
                if price is None:
                    # No bar that day (holiday, or a crypto/equity mismatch).
                    # Carry the last known close rather than dropping the name.
                    prior = [v for k, v in sorted(closes.get(sym, {}).items()) if k <= key]
                    price = prior[-1] if prior else 0.0
                equity += qty * price
            out.append(EquityPoint(ts=day, equity=round(equity, 2)))
        return out[-days:]
