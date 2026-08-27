"""
Turning headlines into decisions.

Three stages, cheapest first:

  1. Prefilter. Keyword and magnitude heuristics in Python. Most market news
     is noise and does not deserve a model call. This stage is deliberately
     generous: it is cheap to pass something through and expensive to miss a
     real event.

  2. Classify. Claude reads the shortlisted items with the position and its
     recorded thesis, and answers a fixed set of questions: does this change
     the thesis, in which direction, how sure are you, and what are the
     probability and payoff if you can estimate them.

  3. Size and gate. Kelly turns an estimate into a weight, and the same
     deterministic risk gate the scheduled run uses decides whether anything
     may happen.

The distinction that matters for a buy and hold portfolio: an event is a
reason to re-read the thesis, not a reason to trade. Most material news should
end in HOLD. The system is built so that is the easy outcome.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, asdict
from typing import Any

from . import config, llm
from .data.news import NewsItem

log = logging.getLogger(__name__)

# Terms that plausibly change an investment case. Weighted because a lawsuit
# and a price target update are not the same size of event.
SIGNAL_TERMS: dict[str, float] = {
    # structural
    "bankruptcy": 1.0, "chapter 11": 1.0, "delisting": 1.0, "fraud": 0.95,
    "accounting": 0.85, "restatement": 0.9, "going concern": 0.95,
    "merger": 0.85, "acquisition": 0.8, "acquire": 0.7, "takeover": 0.85,
    "spin-off": 0.7, "spinoff": 0.7, "buyout": 0.8,
    # legal and regulatory
    "settlement": 0.8, "settles": 0.8, "lawsuit": 0.7, "sues": 0.65,
    "antitrust": 0.8, "sec charges": 0.9, "investigation": 0.7,
    "subpoena": 0.7, "fine": 0.6, "penalty": 0.6, "injunction": 0.7,
    "recall": 0.75, "ban": 0.7, "sanction": 0.75,
    # operating
    "guidance": 0.8, "cuts guidance": 0.95, "raises guidance": 0.85,
    "profit warning": 0.95, "misses": 0.7, "beats": 0.6,
    "earnings": 0.6, "revenue": 0.5, "layoffs": 0.6, "restructuring": 0.65,
    "ceo": 0.7, "cfo": 0.65, "resigns": 0.75, "steps down": 0.75,
    "dividend": 0.6, "buyback": 0.6, "offering": 0.7, "dilution": 0.75,
    # ratings and flow
    "downgrade": 0.6, "upgrade": 0.55, "short seller": 0.8,
    # macro and sector
    "tariff": 0.65, "export controls": 0.75, "rate cut": 0.5, "rate hike": 0.5,
}

# A dollar or percent magnitude in the headline raises the prior.
MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(billion|bn|b|million|mm|m)\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")

PREFILTER_THRESHOLD = 0.55


@dataclass
class Assessment:
    """What one news item means for one position."""
    symbol: str
    item_id: str
    headline: str
    url: str
    published_at: str

    material: bool = False
    materiality: float = 0.0      # 0-1, how much this moves the thesis
    confidence: float = 0.0       # 0-1, how sure the classifier is
    direction: str = "neutral"    # bullish | bearish | neutral
    thesis_impact: str = "none"   # invalidates | weakens | strengthens | none
    action: str = "HOLD"          # OPEN | INCREASE | REDUCE | EXIT | HOLD

    # Kelly inputs. None when the classifier declined to estimate.
    probability: float | None = None
    upside: float | None = None
    downside: float | None = None

    rationale: str = ""
    counter: str = ""
    exit_rule: str = ""
    invalidation: str = ""
    source: str = "heuristic"
    prefilter_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# stage 1: prefilter
# --------------------------------------------------------------------------

def prefilter_score(item: NewsItem) -> float:
    """
    0-1 prior that this headline is worth a model call. Generous by design:
    a false positive costs one API call, a false negative misses the event.
    """
    text = f"{item.headline} {item.summary}".lower()
    score = max((w for term, w in SIGNAL_TERMS.items() if term in text), default=0.0)
    if score == 0.0:
        return 0.0

    money = MONEY.search(text)
    if money:
        amount = float(money.group(1))
        unit = money.group(2).lower()
        billions = amount if unit.startswith(("b", "bn")) else amount / 1000.0
        # A billion-dollar number is material almost regardless of the verb.
        if billions >= 10:
            score = max(score, 0.95)
        elif billions >= 1:
            score = max(score, 0.85)
        elif billions >= 0.1:
            score = max(score, 0.7)

    pct = PERCENT.search(text)
    if pct and float(pct.group(1)) >= 10:
        score = min(1.0, score + 0.08)

    # Stale news is not an event. Anything over two days old is history.
    if item.age_hours > 48:
        score *= 0.4

    return round(min(1.0, score), 3)


def shortlist(items: list[NewsItem], seen: set[str] | None = None,
              threshold: float = PREFILTER_THRESHOLD) -> list[tuple[NewsItem, float]]:
    seen = seen or set()
    out: list[tuple[NewsItem, float]] = []
    for item in items:
        if item.id in seen:
            continue
        s = prefilter_score(item)
        if s >= threshold:
            out.append((item, s))
    out.sort(key=lambda pair: -pair[1])
    return out


# --------------------------------------------------------------------------
# stage 2: classify
# --------------------------------------------------------------------------

def classify(
    candidates: list[tuple[NewsItem, float]],
    positions: dict[str, dict[str, Any]],
    theses: dict[str, Any],
    *,
    model: str | None = None,
) -> list[Assessment]:
    """
    Ask Claude what each shortlisted item means for that position.

    Without an API key every item degrades to a HOLD with no Kelly estimate,
    which surfaces the news in the dashboard for a human to read and changes
    nothing automatically. That is the correct failure mode: no key means no
    automated trading, not automated trading on keyword matches.
    """
    if not candidates:
        return []

    if not llm.available():
        return [
            _no_llm_assessment(item, score,
                               "no ANTHROPIC_API_KEY, so this headline was "
                               "flagged by keyword only and not assessed")
            for item, score in candidates
        ]

    payload = {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "items": [
            {
                "item_id": item.id,
                "symbol": item.symbol,
                "headline": item.headline,
                "summary": item.summary,
                "source": item.source,
                "published_at": item.published_at.isoformat(),
                "age_hours": round(item.age_hours, 1),
                "prefilter_score": score,
                "current_weight": positions.get(item.symbol, {}).get("weight", 0.0),
                "unrealized_plpc": positions.get(item.symbol, {}).get("unrealized_plpc", 0.0),
                "recorded_thesis": theses.get(item.symbol, {}).get("thesis", ""),
                "recorded_invalidation": theses.get(item.symbol, {}).get("invalidation", ""),
                "held": item.symbol in positions,
            }
            for item, score in candidates
        ],
    }

    try:
        text = llm._complete(
            llm.load_prompt("event_review"),
            json.dumps(payload, indent=2, default=str),
            model=model, max_tokens=4000,
        )
        rows = llm._extract_json(text)
        if isinstance(rows, dict):
            rows = rows.get("assessments", [])
    except Exception as e:  # noqa: BLE001 - a classifier failure must not trade
        log.warning("event classification failed, defaulting to HOLD: %s", e)
        return [
            _no_llm_assessment(item, score, f"classifier failed: {e}")
            for item, score in candidates
        ]

    by_id = {item.id: (item, score) for item, score in candidates}
    out: list[Assessment] = []
    for row in rows:
        pair = by_id.get(str(row.get("item_id", "")))
        if pair is None:
            continue
        item, score = pair
        out.append(Assessment(
            symbol=item.symbol,
            item_id=item.id,
            headline=item.headline,
            url=item.url,
            published_at=item.published_at.isoformat(),
            material=bool(row.get("material", False)),
            materiality=_clamp01(row.get("materiality")),
            confidence=_clamp01(row.get("confidence")),
            direction=str(row.get("direction", "neutral")).lower(),
            thesis_impact=str(row.get("thesis_impact", "none")).lower(),
            action=str(row.get("action", "HOLD")).upper(),
            probability=_opt_float(row.get("probability")),
            upside=_opt_float(row.get("upside")),
            downside=_opt_float(row.get("downside")),
            rationale=str(row.get("rationale", "")).strip(),
            counter=str(row.get("counter", "")).strip(),
            exit_rule=str(row.get("exit_rule", "")).strip(),
            invalidation=str(row.get("invalidation", "")).strip(),
            source="claude",
            prefilter_score=score,
        ))

    # Anything the model skipped stays a HOLD rather than vanishing.
    covered = {a.item_id for a in out}
    for item, score in candidates:
        if item.id not in covered:
            out.append(_no_llm_assessment(item, score, "classifier returned no row"))
    return out


def _no_llm_assessment(item: NewsItem, score: float, why: str) -> Assessment:
    return Assessment(
        symbol=item.symbol, item_id=item.id, headline=item.headline,
        url=item.url, published_at=item.published_at.isoformat(),
        material=False, materiality=score, confidence=0.0,
        action="HOLD", rationale=why, source="heuristic", prefilter_score=score,
    )


def _clamp01(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _opt_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
