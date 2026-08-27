"""
Alpaca adapter - paper trading, US equities/ETFs + crypto.

Covers all four universe classes: us_stocks, gold (GLD/IAU/GDX),
oil (USO/BNO/XLE) and crypto. Gold and oil are ETF proxies because Alpaca
does not carry futures; universe.yaml records what each one proxies.

Docs: https://docs.alpaca.markets/us/docs/getting-started
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable

import httpx

from .base import (
    Account, Bar, Broker, BrokerError, BrokerHealth, EquityPoint,
    OrderRequest, OrderResult, Position, Quote,
)

log = logging.getLogger(__name__)

STOCK_BARS = "/v2/stocks/bars"
CRYPTO_BARS = "/v1beta3/crypto/us/bars"
STOCK_TRADES = "/v2/stocks/trades/latest"
CRYPTO_TRADES = "/v1beta3/crypto/us/latest/trades"

STALE_AFTER_SECONDS = 36 * 3600


class AlpacaBroker(Broker):
    adapter = "alpaca"
    supports_notional = True
    supports_fractional = True
    supports_shorting = False
    always_open_classes = ("crypto",)

    def __init__(
        self,
        name: str,
        key_id: str,
        secret_key: str,
        *,
        mode: str = "paper",
        trading_url: str = "https://paper-api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
        supports: tuple[str, ...] = ("us_stocks", "gold", "oil", "crypto",
                                     "sectors"),
        feed: str = "iex",
        timeout: float = 20.0,
    ) -> None:
        if not key_id or not secret_key:
            raise BrokerError(f"{name}: ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set")
        if mode == "live" and "paper-api" not in trading_url:
            log.warning("%s is pointed at a LIVE Alpaca endpoint", name)

        self.name = name
        self.mode = mode  # type: ignore[assignment]
        self.supports = supports
        self.feed = feed
        self._trading = trading_url.rstrip("/")
        self._data = data_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "accept": "application/json",
            },
        )

    # -- symbol handling --------------------------------------------------

    def normalize(self, symbol: str) -> str:
        """Universe form is already what the trading and v1beta3 data APIs want."""
        return symbol.upper()

    def denormalize(self, symbol: str) -> str:
        """Positions come back as BTCUSD; the universe calls it BTC/USD."""
        s = symbol.upper()
        if "/" not in s and len(s) > 4 and s.endswith("USD"):
            return f"{s[:-3]}/USD"
        return s

    @staticmethod
    def _split(symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        syms = [s.upper() for s in symbols]
        return [s for s in syms if "/" not in s], [s for s in syms if "/" in s]

    # -- plumbing ---------------------------------------------------------

    def _get(self, base: str, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            r = self._client.get(f"{base}{path}", params=clean)
        except httpx.HTTPError as e:
            raise BrokerError(f"{self.name}: GET {path} failed: {e}") from e
        if r.status_code == 401:
            raise BrokerError(f"{self.name}: Alpaca rejected the API keys (401)")
        if r.status_code == 403:
            raise BrokerError(f"{self.name}: 403 - this key has no access to {path}")
        if r.status_code == 429:
            raise BrokerError(f"{self.name}: rate limited by Alpaca on {path}")
        if r.status_code >= 400:
            raise BrokerError(f"{self.name}: {r.status_code} from {path}: {r.text[:300]}")
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self._client.post(f"{self._trading}{path}", json=payload)
        except httpx.HTTPError as e:
            raise BrokerError(f"{self.name}: POST {path} failed: {e}") from e
        if r.status_code >= 400:
            raise BrokerError(f"{self.name}: order rejected ({r.status_code}): {r.text[:300]}")
        return r.json()

    # -- required interface -----------------------------------------------

    def health(self) -> BrokerHealth:
        try:
            a = self._get(self._trading, "/v2/account")
        except BrokerError as e:
            return BrokerHealth(self.name, False, self.mode, str(e), self.supports)
        detail = (
            f"account {a.get('account_number', '?')} "
            f"status={a.get('status', '?')} "
            f"equity=${float(a.get('equity', 0)):,.2f}"
        )
        if a.get("trading_blocked") or a.get("account_blocked"):
            return BrokerHealth(self.name, False, self.mode, f"trading blocked - {detail}", self.supports)
        return BrokerHealth(self.name, True, self.mode, detail, self.supports)

    def get_account(self) -> Account:
        a = self._get(self._trading, "/v2/account")
        return Account(
            equity=float(a["equity"]),
            cash=float(a["cash"]),
            buying_power=float(a["buying_power"]),
            currency=a.get("currency", "USD"),
            broker=self.name,
            mode=self.mode,
        )

    def get_positions(self) -> list[Position]:
        raw = self._get(self._trading, "/v2/positions")
        return [
            Position(
                symbol=self.denormalize(p["symbol"]),
                qty=float(p["qty"]),
                avg_cost=float(p["avg_entry_price"]),
                market_value=float(p["market_value"]),
                unrealized_pl=float(p.get("unrealized_pl") or 0.0),
                unrealized_plpc=float(p.get("unrealized_plpc") or 0.0),
                asset_class="crypto" if p.get("asset_class") == "crypto" else "equity",
                broker=self.name,
            )
            for p in raw
        ]

    def get_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]:
        equities, cryptos = self._split(symbols)
        # Ask generously in calendar days so `days` trading days come back.
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days * 1.7) + 10)).date()
        out: dict[str, list[Bar]] = {}
        if equities:
            out.update(self._paged_bars(STOCK_BARS, equities, start, feed=self.feed))
        if cryptos:
            out.update(self._paged_bars(CRYPTO_BARS, cryptos, start, feed=None))
        return out

    def _paged_bars(
        self, path: str, symbols: list[str], start: dt.date, *, feed: str | None
    ) -> dict[str, list[Bar]]:
        collected: dict[str, list[Bar]] = {s: [] for s in symbols}
        page: str | None = None
        for _ in range(20):  # hard page cap so a bad token can never spin
            body = self._get(
                self._data, path,
                symbols=",".join(symbols),
                timeframe="1Day",
                start=start.isoformat(),
                limit=10000,
                adjustment="split",
                feed=feed,
                page_token=page,
            )
            for sym, rows in (body.get("bars") or {}).items():
                collected.setdefault(sym, []).extend(
                    Bar(
                        ts=_parse_ts(r["t"]),
                        open=float(r["o"]), high=float(r["h"]), low=float(r["l"]),
                        close=float(r["c"]), volume=float(r["v"]),
                    )
                    for r in rows
                )
            page = body.get("next_page_token")
            if not page:
                break
        return {s: sorted(v, key=lambda b: b.ts) for s, v in collected.items()}

    def get_quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        equities, cryptos = self._split(symbols)
        out: dict[str, Quote] = {}
        now = dt.datetime.now(dt.timezone.utc)

        for path, syms, feed in (
            (STOCK_TRADES, equities, self.feed),
            (CRYPTO_TRADES, cryptos, None),
        ):
            if not syms:
                continue
            body = self._get(self._data, path, symbols=",".join(syms), feed=feed)
            for sym, t in (body.get("trades") or {}).items():
                ts = _parse_ts(t["t"])
                out[sym] = Quote(sym, float(t["p"]), ts,
                                 stale=(now - ts).total_seconds() > STALE_AFTER_SECONDS)

        # Anything the trade endpoint had nothing for: last daily bar, marked stale.
        missing = [s for s in equities + cryptos if s not in out]
        if missing:
            for sym, bars in self.get_bars(missing, 5).items():
                if bars:
                    out[sym] = Quote(sym, bars[-1].close, bars[-1].ts, stale=True)
        return out

    def submit_order(self, order: OrderRequest) -> OrderResult:
        sym = self.normalize(order.symbol)
        is_crypto = "/" in sym
        payload: dict[str, Any] = {
            "symbol": sym,
            "side": order.side,
            "type": order.type,
            # Alpaca rejects tif=day on crypto; gtc is the portable choice.
            "time_in_force": "gtc" if is_crypto else order.time_in_force,
        }
        if order.notional is not None:
            payload["notional"] = round(order.notional, 2)
        else:
            payload["qty"] = order.qty
        if order.type == "limit":
            payload["limit_price"] = order.limit_price
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        return self._order_result(self._post("/v2/orders", payload),
                                  note="submitted to Alpaca paper")

    def get_orders(self, status: str = "all", limit: int = 50) -> list[OrderResult]:
        raw = self._get(self._trading, "/v2/orders", status=status, limit=limit,
                        direction="desc", nested="false")
        return [self._order_result(d) for d in raw]

    def cancel_order(self, order_id: str) -> bool:
        try:
            r = self._client.delete(f"{self._trading}/v2/orders/{order_id}")
        except httpx.HTTPError as e:
            raise BrokerError(f"{self.name}: cancel failed: {e}") from e
        return r.status_code in (200, 204)

    def is_market_open(self, asset_class: str = "us_stocks") -> bool:
        if asset_class in self.always_open_classes:
            return True
        return bool(self._get(self._trading, "/v2/clock").get("is_open"))

    def get_portfolio_history(self, days: int = 90) -> list[EquityPoint]:
        """
        Alpaca keeps the equity series itself, so the dashboard gets a real
        curve from the first day rather than one point per saved run.
        """
        period = "1M" if days <= 31 else ("3M" if days <= 92 else "1A")
        try:
            body = self._get(self._trading, "/v2/account/portfolio/history",
                             period=period, timeframe="1D", extended_hours="false")
        except BrokerError as e:
            log.warning("%s: portfolio history unavailable: %s", self.name, e)
            return []

        stamps = body.get("timestamp") or []
        equities = body.get("equity") or []
        out: list[EquityPoint] = []
        for ts, eq in zip(stamps, equities):
            # Alpaca sends null equity for days the account did not exist yet.
            if eq is None:
                continue
            out.append(EquityPoint(
                ts=dt.datetime.fromtimestamp(int(ts), dt.timezone.utc),
                equity=float(eq),
            ))
        return out[-days:]

    # -- helpers ----------------------------------------------------------

    def _order_result(self, d: dict, note: str = "") -> OrderResult:
        submitted = d.get("submitted_at") or d.get("created_at")
        ts = _parse_ts(submitted) if submitted else dt.datetime.now(dt.timezone.utc)
        filled_price = d.get("filled_avg_price")
        return OrderResult(
            id=str(d.get("id", "")),
            symbol=self.denormalize(d.get("symbol", "")),
            side=d.get("side", "buy"),
            status=d.get("status", "unknown"),
            submitted_at=ts,
            filled_qty=float(d.get("filled_qty") or 0.0),
            filled_avg_price=float(filled_price) if filled_price else None,
            broker=self.name,
            note=note,
            raw=d,
        )

    def close(self) -> None:
        self._client.close()


def _parse_ts(value: str) -> dt.datetime:
    """Alpaca returns RFC3339 with Z; sometimes with nanosecond precision."""
    s = value.replace("Z", "+00:00")
    if "." in s:
        head, _, tail = s.partition(".")
        frac, sign, off = _split_offset(tail)
        s = f"{head}.{frac[:6]}{sign}{off}"
    parsed = dt.datetime.fromisoformat(s)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _split_offset(tail: str) -> tuple[str, str, str]:
    for sign in ("+", "-"):
        if sign in tail:
            frac, _, off = tail.partition(sign)
            return frac, sign, off
    return tail, "", ""
