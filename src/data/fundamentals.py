"""
Fundamentals, from yfinance.

This closes the gap every memo has been reporting: valuation and catalyst are
30% of the scoring rubric and both have been scoring neutral and printing
UNSCORED, because the free Alpaca feed carries prices and volume and nothing
else. yfinance is free, needs no key, and carries what the rubric asks for.

It is also scraped rather than contracted, so it goes down, rate limits, and
occasionally returns nonsense. Everything here is defensive:

  - results cache to disk with a TTL, so a run does not depend on a live fetch
  - a missing field stays missing and the component stays UNSCORED
  - a fetch failure degrades that symbol, never the run

Nothing here decides anything. It produces numbers that src/score.py turns
into sub-scores under the locked rubric.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

from ..config import DATA_DIR

log = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "fundamentals.json"
CACHE_TTL_HOURS = 20  # one trading day, so a scheduled run fetches once


@dataclass
class Fundamentals:
    symbol: str
    as_of: str = ""

    # valuation
    trailing_pe: float | None = None
    forward_pe: float | None = None
    peg: float | None = None
    price_to_book: float | None = None
    ev_to_ebitda: float | None = None

    # quality
    profit_margin: float | None = None
    operating_margin: float | None = None
    return_on_equity: float | None = None
    debt_to_equity: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None

    # catalyst
    next_earnings_date: str | None = None
    days_to_earnings: int | None = None
    dividend_yield: float | None = None

    # classification, used for real sector concentration
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    quote_type: str | None = None   # EQUITY, ETF, MUTUALFUND, CRYPTOCURRENCY

    error: str = ""

    @property
    def is_operating_company(self) -> bool:
        """
        Only a real business has earnings, margins and a book value that mean
        anything. An ETF has a price to book because it holds things that do,
        and scoring GLD on it would put a fabricated valuation into the rubric.
        """
        return (self.quote_type or "EQUITY").upper() == "EQUITY"

    @property
    def usable(self) -> bool:
        return (
            not self.error
            and self.is_operating_company
            and any(v is not None for v in
                    (self.trailing_pe, self.forward_pe, self.price_to_book,
                     self.profit_margin))
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _fresh(entry: dict[str, Any], ttl_hours: int) -> bool:
    raw = entry.get("as_of")
    if not raw:
        return False
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(raw))
    except ValueError:
        return False
    return age.total_seconds() < ttl_hours * 3600


def _num(value: Any) -> float | None:
    """yfinance returns None, nan, and occasionally strings. Normalise."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def fetch(symbols: Iterable[str], *, ttl_hours: int = CACHE_TTL_HOURS,
          force: bool = False) -> dict[str, Fundamentals]:
    """
    Fundamentals for each symbol, cached. Crypto pairs are skipped: there is no
    price to earnings ratio for bitcoin, and pretending otherwise would put a
    fabricated number into the rubric.
    """
    # A bare string is iterable, so fetch("NVDA") would quietly return four
    # entries keyed N, V, D and A, each looked up as a real ticker. That is
    # worse than an error because the result still looks like data.
    if isinstance(symbols, str):
        raise TypeError(
            f"fetch() takes a sequence of symbols, not the string {symbols!r}. "
            f"Pass [{symbols!r}] instead.")

    cache = _load_cache()
    out: dict[str, Fundamentals] = {}
    to_fetch: list[str] = []

    for raw in symbols:
        sym = raw.upper()
        if "/" in sym:
            out[sym] = Fundamentals(sym, error="no fundamentals for a crypto pair")
            continue
        entry = cache.get(sym)
        if entry and _fresh(entry, ttl_hours) and not force:
            out[sym] = Fundamentals(**{k: v for k, v in entry.items()
                                       if k in Fundamentals.__annotations__})
        else:
            to_fetch.append(sym)

    if not to_fetch:
        return out

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance is not installed, fundamentals unavailable")
        for sym in to_fetch:
            out[sym] = Fundamentals(sym, error="yfinance not installed")
        return out

    now = dt.datetime.now(dt.timezone.utc)
    for sym in to_fetch:
        try:
            info = yf.Ticker(sym).info or {}
        except Exception as e:  # noqa: BLE001 - yfinance raises many things
            log.warning("fundamentals fetch failed for %s: %s", sym, e)
            out[sym] = Fundamentals(sym, as_of=now.isoformat(), error=str(e)[:200])
            continue

        f = Fundamentals(
            symbol=sym,
            as_of=now.isoformat(),
            trailing_pe=_num(info.get("trailingPE")),
            forward_pe=_num(info.get("forwardPE")),
            peg=_num(info.get("trailingPegRatio") or info.get("pegRatio")),
            price_to_book=_num(info.get("priceToBook")),
            ev_to_ebitda=_num(info.get("enterpriseToEbitda")),
            profit_margin=_num(info.get("profitMargins")),
            operating_margin=_num(info.get("operatingMargins")),
            return_on_equity=_num(info.get("returnOnEquity")),
            debt_to_equity=_num(info.get("debtToEquity")),
            revenue_growth=_num(info.get("revenueGrowth")),
            earnings_growth=_num(info.get("earningsGrowth")),
            dividend_yield=_num(info.get("dividendYield")),
            sector=info.get("sector"),
            industry=info.get("industry"),
            market_cap=_num(info.get("marketCap")),
            quote_type=info.get("quoteType"),
        )

        stamp = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if stamp:
            try:
                when = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc)
                f.next_earnings_date = when.date().isoformat()
                f.days_to_earnings = (when.date() - now.date()).days
            except (TypeError, ValueError, OSError):
                pass

        out[sym] = f
        cache[sym] = f.to_dict()

    _save_cache(cache)
    return out


# --------------------------------------------------------------------------
# scoring helpers. These convert raw fundamentals into 0-100, or return None
# when there is nothing to convert, which keeps the component UNSCORED rather
# than inventing a neutral that looks like a real reading.
# --------------------------------------------------------------------------

def valuation_score(f: Fundamentals) -> float | None:
    """
    Cheaper scores higher, on absolute bands rather than a cross-sectional
    rank, so a whole expensive universe cannot make its cheapest member look
    like a bargain.
    """
    if not f.usable:
        return None
    parts: list[float] = []

    pe = f.forward_pe or f.trailing_pe
    if pe is not None and pe > 0:
        # 10x scores 100, 40x scores 0, linear between.
        parts.append(_band(pe, best=10.0, worst=40.0))

    if f.peg is not None and f.peg > 0:
        parts.append(_band(f.peg, best=0.8, worst=3.0))

    if f.price_to_book is not None and f.price_to_book > 0:
        parts.append(_band(f.price_to_book, best=1.5, worst=12.0))

    if f.ev_to_ebitda is not None and f.ev_to_ebitda > 0:
        parts.append(_band(f.ev_to_ebitda, best=8.0, worst=30.0))

    return round(sum(parts) / len(parts), 2) if parts else None


def quality_score(f: Fundamentals) -> float | None:
    """Margins, returns and leverage. A cheap bad business is still bad."""
    if not f.usable:
        return None
    parts: list[float] = []
    if f.profit_margin is not None:
        parts.append(_band(f.profit_margin, best=0.30, worst=0.0, higher_is_better=True))
    if f.operating_margin is not None:
        parts.append(_band(f.operating_margin, best=0.35, worst=0.0, higher_is_better=True))
    if f.return_on_equity is not None:
        parts.append(_band(f.return_on_equity, best=0.30, worst=0.0, higher_is_better=True))
    if f.debt_to_equity is not None:
        parts.append(_band(f.debt_to_equity, best=30.0, worst=200.0))
    if f.revenue_growth is not None:
        parts.append(_band(f.revenue_growth, best=0.25, worst=-0.05, higher_is_better=True))
    return round(sum(parts) / len(parts), 2) if parts else None


def catalyst_score(f: Fundamentals) -> float | None:
    """
    For a buy and hold portfolio a near earnings date is a reason for caution,
    not excitement: it is a known event that can reprice the position before
    any thesis has time to play out.
    """
    if f.error or not f.is_operating_company:
        return None
    parts: list[float] = []
    if f.days_to_earnings is not None:
        d = f.days_to_earnings
        if d < 0:
            parts.append(70.0)          # just reported, the air is clear
        elif d <= 7:
            parts.append(30.0)          # inside the event window
        elif d <= 21:
            parts.append(55.0)
        else:
            parts.append(75.0)
    if f.earnings_growth is not None:
        parts.append(_band(f.earnings_growth, best=0.25, worst=-0.15,
                           higher_is_better=True))
    return round(sum(parts) / len(parts), 2) if parts else None


def _band(value: float, *, best: float, worst: float,
          higher_is_better: bool = False) -> float:
    """Map a value onto 0-100 between a best and worst anchor, clamped."""
    if higher_is_better:
        if value >= best:
            return 100.0
        if value <= worst:
            return 0.0
        return 100.0 * (value - worst) / (best - worst)
    if value <= best:
        return 100.0
    if value >= worst:
        return 0.0
    return 100.0 * (worst - value) / (worst - best)
