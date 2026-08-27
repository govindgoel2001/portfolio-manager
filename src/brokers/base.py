"""
The broker seam.

Every broker the system will ever talk to implements `Broker`. Nothing else in
the codebase imports a concrete adapter. The pipeline, risk engine, API and
dashboard all receive a `Broker` from the registry and never learn which one
they got.

To add Dhan or Hyperliquid:
    1. Create src/brokers/dhan.py with `class DhanBroker(Broker)`.
    2. Add it to config/brokers.yaml and route an asset class to it.
    3. `python -m src.brokers.registry --check` verifies the protocol.

That is the whole procedure. There is deliberately no other integration point.
"""
from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
Mode = Literal["paper", "live"]


# --------------------------------------------------------------------------
# Value types. Adapters translate their vendor's JSON into these; the rest of
# the system only ever sees these.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: dt.datetime
    stale: bool = False

    @property
    def age_hours(self) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        ts = self.ts if self.ts.tzinfo else self.ts.replace(tzinfo=dt.timezone.utc)
        return (now - ts).total_seconds() / 3600.0


@dataclass(frozen=True)
class EquityPoint:
    """One point on the account's equity curve, as the broker reports it."""
    ts: dt.datetime
    equity: float

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.ts.date().isoformat(), "equity": round(self.equity, 2)}


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_cost: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float
    asset_class: str = "unknown"
    broker: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"
    broker: str = "unknown"
    mode: Mode = "paper"

    @property
    def cash_weight(self) -> float:
        return self.cash / self.equity if self.equity else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cash_weight"] = round(self.cash_weight, 4)
        return d


@dataclass(frozen=True)
class OrderRequest:
    """
    Notional-first by design: allocation is expressed in weights, weights become
    dollars, and dollars are what we send. Adapters that cannot do notional
    orders convert to quantity using the last quote and say so in the result.
    """
    symbol: str
    side: Side
    notional: float | None = None
    qty: float | None = None
    type: OrderType = "market"
    limit_price: float | None = None
    time_in_force: str = "day"
    client_order_id: str | None = None
    asset_class: str = "unknown"

    def __post_init__(self) -> None:
        if (self.notional is None) == (self.qty is None):
            raise ValueError(f"{self.symbol}: pass exactly one of notional or qty")
        if self.type == "limit" and self.limit_price is None:
            raise ValueError(f"{self.symbol}: limit order needs limit_price")


@dataclass(frozen=True)
class OrderResult:
    id: str
    symbol: str
    side: Side
    status: str
    submitted_at: dt.datetime
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    broker: str = "unknown"
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["submitted_at"] = self.submitted_at.isoformat()
        d.pop("raw", None)
        return d


@dataclass(frozen=True)
class BrokerHealth:
    name: str
    ok: bool
    mode: Mode
    detail: str
    supports: tuple[str, ...] = ()


class BrokerError(RuntimeError):
    """Anything the adapter could not do. Never leaks vendor exception types."""


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------

class Broker(abc.ABC):
    """
    Implement all abstract methods. Optional capability flags let the pipeline
    adapt without special-casing any vendor by name.
    """

    #: registry key, e.g. "alpaca_paper"
    name: str = "unnamed"
    #: adapter family, e.g. "alpaca"
    adapter: str = "base"
    mode: Mode = "paper"
    #: asset classes from universe.yaml this broker can handle
    supports: tuple[str, ...] = ()

    # -- capability flags -------------------------------------------------
    supports_notional: bool = True
    supports_fractional: bool = True
    supports_shorting: bool = False
    #: classes that trade 24/7 and so ignore market-hours checks
    always_open_classes: tuple[str, ...] = ("crypto",)

    # -- required ---------------------------------------------------------

    @abc.abstractmethod
    def health(self) -> BrokerHealth: ...

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def get_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]: ...

    @abc.abstractmethod
    def get_quotes(self, symbols: Iterable[str]) -> dict[str, Quote]: ...

    @abc.abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    def get_orders(self, status: str = "all", limit: int = 50) -> list[OrderResult]: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abc.abstractmethod
    def is_market_open(self, asset_class: str = "us_stocks") -> bool: ...

    # -- provided ---------------------------------------------------------

    def normalize(self, symbol: str) -> str:
        """Universe symbol -> vendor symbol. Override where they differ."""
        return symbol

    def denormalize(self, symbol: str) -> str:
        """Vendor symbol -> universe symbol."""
        return symbol

    def can_trade(self, asset_class: str) -> bool:
        return asset_class in self.supports

    def get_portfolio_history(self, days: int = 90) -> list[EquityPoint]:
        """
        The account's equity over time, as the venue records it.

        Optional. An adapter that cannot supply this returns an empty list and
        the dashboard falls back to one point per saved run, which is much
        coarser. Implement it wherever the venue has an equivalent endpoint.
        """
        return []

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name} mode={self.mode}>"


REQUIRED_METHODS = (
    "health", "get_account", "get_positions", "get_bars", "get_quotes",
    "submit_order", "get_orders", "cancel_order", "is_market_open",
)


def verify_adapter(cls: type) -> list[str]:
    """
    Structural check on the class, used by `registry --check`.

    Deliberately does not check `supports`: that is per-instance config read
    from brokers.yaml, not a property of the class. The registry validates it
    at construction time with `verify_instance`.
    """
    problems: list[str] = []
    if not issubclass(cls, Broker):
        problems.append(f"{cls.__name__} does not subclass Broker")
    for meth in REQUIRED_METHODS:
        fn = getattr(cls, meth, None)
        if fn is None:
            problems.append(f"{cls.__name__} is missing {meth}()")
        elif getattr(fn, "__isabstractmethod__", False):
            problems.append(f"{cls.__name__} leaves {meth}() abstract")
    return problems


def verify_instance(broker: "Broker") -> list[str]:
    """Runtime check on a constructed broker, before it is routed any symbols."""
    problems: list[str] = []
    if not broker.supports:
        problems.append(
            f"{broker.name} declares no supported asset classes "
            f"- set `supports:` for it in config/brokers.yaml"
        )
    if broker.name == "unnamed":
        problems.append(f"{type(broker).__name__} did not set self.name")
    return problems
