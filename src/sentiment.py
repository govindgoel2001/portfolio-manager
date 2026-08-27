"""
AI sentiment as a scored rubric component.

Everything else in the rubric is measured: a price ratio, a margin, a realised
volatility. This one is a model's reading of what people are saying about a
company, which is a genuinely different kind of input and has to be treated as
one.

Three rules follow from that:

  Small weight. Sentiment is noisy, it mean-reverts, and for a portfolio held
  for months it should nudge a decision rather than drive one. It carries less
  weight than valuation or quality.

  Never invented. No news means no sentiment, the component is UNSCORED, and
  the renormaliser gives its weight to the components that were measured. A
  neutral 50 dressed up as a reading is worse than an honest gap.

  Always attributable. Every score keeps the headlines it was derived from, so
  a memo can show its work and you can tell "the market hates this" from "one
  aggregator ran three rewrites of the same story".

Cached with a TTL so a scoring run does not depend on a live model call, and
so the same headlines are not re-scored every twenty minutes.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

from . import llm
from .config import DATA_DIR
from .data.news import NewsItem

log = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "sentiment.json"
CACHE_TTL_HOURS = 6
MAX_HEADLINES = 12
MIN_HEADLINES = 2


@dataclass
class Sentiment:
    symbol: str
    score: float | None = None          # 0-100, 50 is genuinely neutral
    confidence: float = 0.0             # 0-1
    label: str = "unscored"             # bearish | neutral | bullish | unscored
    rationale: str = ""
    headline_count: int = 0
    distinct_sources: int = 0
    as_of: str = ""
    headlines: list[str] = field(default_factory=list)
    source: str = "none"                # claude | none
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.score is not None and self.headline_count >= MIN_HEADLINES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

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
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(raw)
    except ValueError:
        return False
    return age.total_seconds() < ttl_hours * 3600


def cached(symbol: str, *, ttl_hours: int = CACHE_TTL_HOURS) -> Sentiment | None:
    entry = _load_cache().get(symbol.upper())
    if entry and _fresh(entry, ttl_hours):
        return Sentiment(**{k: v for k, v in entry.items()
                            if k in Sentiment.__annotations__})
    return None


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_symbols(
    items_by_symbol: dict[str, list[NewsItem]],
    *,
    ttl_hours: int = CACHE_TTL_HOURS,
    force: bool = False,
    model: str | None = None,
) -> dict[str, Sentiment]:
    """
    One model call for the whole batch rather than one per symbol. Symbols with
    too little news are returned unscored without spending anything on them.
    """
    cache = _load_cache()
    out: dict[str, Sentiment] = {}
    to_score: dict[str, list[NewsItem]] = {}

    for raw_symbol, items in items_by_symbol.items():
        symbol = raw_symbol.upper()
        entry = cache.get(symbol)
        if entry and _fresh(entry, ttl_hours) and not force:
            out[symbol] = Sentiment(**{k: v for k, v in entry.items()
                                       if k in Sentiment.__annotations__})
            continue
        fresh_items = [i for i in items if i.age_hours <= 72]
        if len(fresh_items) < MIN_HEADLINES:
            out[symbol] = Sentiment(
                symbol=symbol, headline_count=len(fresh_items),
                as_of=_now(),
                note=f"only {len(fresh_items)} headlines in the last 72 hours, "
                     f"need {MIN_HEADLINES}",
            )
            continue
        to_score[symbol] = fresh_items[:MAX_HEADLINES]

    if not to_score:
        return out

    if not llm.available():
        for symbol, items in to_score.items():
            out[symbol] = Sentiment(
                symbol=symbol, headline_count=len(items), as_of=_now(),
                note="no ANTHROPIC_API_KEY, so sentiment was not assessed",
            )
        return out

    payload = {
        "as_of": _now(),
        "symbols": [
            {
                "symbol": symbol,
                "headlines": [
                    {"headline": i.headline, "summary": i.summary[:280],
                     "source": i.source, "age_hours": round(i.age_hours, 1)}
                    for i in items
                ],
            }
            for symbol, items in to_score.items()
        ],
    }

    try:
        text = llm._complete(
            llm.load_prompt("sentiment"),
            json.dumps(payload, indent=2, default=str),
            model=model, max_tokens=3000,
        )
        rows = llm._extract_json(text)
        if isinstance(rows, dict):
            rows = rows.get("sentiment", [])
    except Exception as e:  # noqa: BLE001 - sentiment must never break a run
        log.warning("sentiment scoring failed, leaving symbols unscored: %s", e)
        for symbol, items in to_score.items():
            out[symbol] = Sentiment(symbol=symbol, headline_count=len(items),
                                    as_of=_now(), note=f"scoring failed: {e}")
        return out

    by_symbol = {str(r.get("symbol", "")).upper(): r for r in rows}
    for symbol, items in to_score.items():
        row = by_symbol.get(symbol)
        if not row:
            out[symbol] = Sentiment(symbol=symbol, headline_count=len(items),
                                    as_of=_now(),
                                    note="model returned no row for this symbol")
            continue

        raw = row.get("score")
        score = None
        try:
            score = max(0.0, min(100.0, float(raw)))
        except (TypeError, ValueError):
            pass

        sources = {i.source for i in items}
        s = Sentiment(
            symbol=symbol,
            score=score,
            confidence=_clamp01(row.get("confidence")),
            label=str(row.get("label", "neutral")).lower(),
            rationale=str(row.get("rationale", "")).strip(),
            headline_count=len(items),
            distinct_sources=len(sources),
            as_of=_now(),
            headlines=[i.headline for i in items[:5]],
            source="claude",
        )

        # One outlet running the same story five times is one opinion, not five.
        if s.distinct_sources < 2 and s.headline_count >= 3:
            s.note = (f"all {s.headline_count} headlines came from one source, "
                      f"so this is one outlet's view rather than a consensus")
            s.confidence = min(s.confidence, 0.4)

        out[symbol] = s
        cache[symbol] = s.to_dict()

    _save_cache(cache)
    return out


def sentiment_score(s: Sentiment | None) -> float | None:
    """
    Rubric adapter. Returns None when there is nothing to score, which keeps
    the component UNSCORED instead of contributing a fabricated neutral.

    A low confidence reading is pulled toward neutral rather than dropped:
    the model saw something, it is just not sure, and shrinking is the honest
    representation of that.
    """
    if s is None or not s.usable:
        return None
    if s.confidence < 0.25:
        return None
    shrunk = 50.0 + (s.score - 50.0) * s.confidence
    return round(shrunk, 2)


def group_by_symbol(items: Iterable[NewsItem]) -> dict[str, list[NewsItem]]:
    out: dict[str, list[NewsItem]] = {}
    for item in items:
        out.setdefault(item.symbol.upper(), []).append(item)
    for symbol in out:
        out[symbol].sort(key=lambda i: i.published_at, reverse=True)
    return out


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _clamp01(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0
