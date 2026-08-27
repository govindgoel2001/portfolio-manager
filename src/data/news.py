"""
News providers.

Same shape as the broker seam: a protocol, adapters discovered by name, and a
registry that falls back rather than crashing when a source has no credentials.

Two adapters ship:

  alpaca    the news endpoint on the market data API. Free with any Alpaca
            key, symbol-tagged, which is what makes it the right default once
            the keys exist.
  firecrawl the REST search API, used when there is no Alpaca key. Slower and
            not symbol-tagged at source, so the symbol is inferred from the
            query that produced the hit.

A headline is not a signal. Everything here returns raw items; deciding
whether one matters is src/events.py, and deciding whether to act on it is
src/risk.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    id: str
    symbol: str
    headline: str
    summary: str
    source: str
    url: str
    published_at: dt.datetime

    @property
    def age_hours(self) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        ts = self.published_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return (now - ts).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published_at"] = self.published_at.isoformat()
        d["age_hours"] = round(self.age_hours, 2)
        return d


class NewsError(RuntimeError):
    pass


class NewsProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch(self, symbols: Iterable[str], since: dt.datetime,
              limit: int = 50) -> list[NewsItem]: ...


def _stable_id(url: str, headline: str) -> str:
    """Same story from the same source is the same id across runs."""
    return hashlib.sha256(f"{url}|{headline}".encode()).hexdigest()[:16]


def _parse_ts(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------

class AlpacaNews:
    """https://data.alpaca.markets/v1beta1/news, symbol tagged at source."""

    name = "alpaca"

    def __init__(self, key_id: str = "", secret: str = "",
                 base: str = "https://data.alpaca.markets") -> None:
        self.key_id = key_id or os.environ.get("ALPACA_KEY_ID", "")
        self.secret = secret or os.environ.get("ALPACA_SECRET_KEY", "")
        self.base = base.rstrip("/")

    def available(self) -> bool:
        return bool(self.key_id and self.secret)

    def fetch(self, symbols: Iterable[str], since: dt.datetime,
              limit: int = 50) -> list[NewsItem]:
        if not self.available():
            raise NewsError("alpaca news needs ALPACA_KEY_ID and ALPACA_SECRET_KEY")
        # The news endpoint takes equity tickers. Crypto pairs are not tagged
        # there, so they are dropped rather than silently returning nothing.
        tickers = [s.upper() for s in symbols if "/" not in s]
        if not tickers:
            return []

        headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
            "accept": "application/json",
        }
        params = {
            "symbols": ",".join(tickers),
            "start": since.astimezone(dt.timezone.utc).isoformat(),
            "limit": min(limit, 50),
            "sort": "desc",
            "include_content": "false",
        }
        try:
            r = httpx.get(f"{self.base}/v1beta1/news", headers=headers,
                          params=params, timeout=20.0)
        except httpx.HTTPError as e:
            raise NewsError(f"alpaca news request failed: {e}") from e
        if r.status_code >= 400:
            raise NewsError(f"alpaca news {r.status_code}: {r.text[:200]}")

        out: list[NewsItem] = []
        for n in r.json().get("news", []):
            url = n.get("url", "")
            headline = n.get("headline", "")
            for sym in (n.get("symbols") or ["?"]):
                if sym.upper() not in tickers:
                    continue
                out.append(NewsItem(
                    id=_stable_id(url, headline),
                    symbol=sym.upper(),
                    headline=headline,
                    summary=(n.get("summary") or "")[:600],
                    source=n.get("source", "alpaca"),
                    url=url,
                    published_at=_parse_ts(n.get("created_at", "")),
                ))
        return out


class FirecrawlNews:
    """
    Fallback for when there is no Alpaca key.

    Firecrawl searches the open web, so results are not symbol tagged. The
    symbol is attributed from the query that found the item, which means a
    story mentioning two companies is attributed to whichever one was searched.
    Good enough to trigger a re-read, not good enough to trade blind, which is
    why the classifier still has to agree it is material.
    """

    name = "firecrawl"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self, symbols: Iterable[str], since: dt.datetime,
              limit: int = 50) -> list[NewsItem]:
        if not self.available():
            raise NewsError("firecrawl news needs FIRECRAWL_API_KEY")

        out: list[NewsItem] = []
        per_symbol = max(2, limit // max(1, len(list(symbols)) or 1))
        for sym in symbols:
            clean = sym.replace("/USD", "")
            try:
                r = httpx.post(
                    "https://api.firecrawl.dev/v1/search",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "query": f"{clean} stock news",
                        "limit": per_symbol,
                        "tbs": "qdr:d",  # last 24 hours
                    },
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                log.warning("firecrawl search failed for %s: %s", sym, e)
                continue
            if r.status_code >= 400:
                log.warning("firecrawl %s for %s: %s", r.status_code, sym, r.text[:160])
                continue

            for hit in (r.json().get("data") or [])[:per_symbol]:
                url = hit.get("url", "")
                title = hit.get("title", "")
                if not title:
                    continue
                out.append(NewsItem(
                    id=_stable_id(url, title),
                    symbol=sym.upper(),
                    headline=title,
                    summary=(hit.get("description") or "")[:600],
                    source="firecrawl",
                    url=url,
                    # Search results rarely carry a timestamp. Treat as now and
                    # let the seen-id ledger stop it being re-processed.
                    published_at=dt.datetime.now(dt.timezone.utc),
                ))
        return out


# --------------------------------------------------------------------------

ADAPTERS: dict[str, type] = {"alpaca": AlpacaNews, "firecrawl": FirecrawlNews}


def resolve(preferred: list[str] | None = None) -> NewsProvider | None:
    """First available provider in preference order, or None."""
    for name in (preferred or ["alpaca", "firecrawl"]):
        cls = ADAPTERS.get(name)
        if cls is None:
            continue
        provider = cls()
        if provider.available():
            return provider
    return None


def fetch_all(symbols: list[str], since: dt.datetime, *,
              limit: int = 50, preferred: list[str] | None = None
              ) -> tuple[list[NewsItem], str]:
    """Returns (items, provider_name). Never raises for a missing provider."""
    provider = resolve(preferred)
    if provider is None:
        return [], "none"
    try:
        return provider.fetch(symbols, since, limit), provider.name
    except NewsError as e:
        log.warning("news fetch failed on %s: %s", provider.name, e)
        return [], provider.name
