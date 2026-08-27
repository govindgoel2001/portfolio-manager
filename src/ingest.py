"""
Filling the retrieval corpus.

Nothing retrieves what was never written down. This is the write side of
src/rag.py, called from the places that already hold the material: the daily
run when it fetches news and writes a memo, and the committee endpoint when it
pulls disclosures.

Everything carries a source and a date, because a passage handed to a model
without provenance is a passage it will cite as fact and neither of you will be
able to say where it came from.

Ingestion is best effort by design. A retrieval index is an accelerator, not a
dependency: if it fails, the run still has the fundamentals, the rubric and the
disclosures it was always going to have, and the only loss is that the models
see less history. So every function here swallows its own failures and says so
in the log rather than taking a run down with it.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from . import rag

log = logging.getLogger(__name__)


def _safe(what: str, fn, *args, **kwargs) -> int:
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - never take a run down for the index
        log.warning("ingesting %s failed, continuing without it: %s", what, e)
        return 0


def news(items: Iterable[Any]) -> int:
    """News items from src/data/news.py. Deduped by content inside the index."""
    def run() -> int:
        index = rag.index()
        added = 0
        for item in items:
            body = f"{item.headline}. {item.summary or ''}".strip()
            added += index.add(
                "news", body,
                symbol=getattr(item, "symbol", ""),
                at=str(getattr(item, "published_at", "")),
                meta={"source": getattr(item, "source", ""),
                      "url": getattr(item, "url", "")},
            )
        if added:
            index.save()
        return added
    return _safe("news", run)


def memo(run_id: str, text: str) -> int:
    """
    The decision memo. Worth indexing because it is the only record of what
    this system believed at a point in time and why, which is exactly the
    context a later committee has no other way to get.
    """
    def run() -> int:
        index = rag.index()
        added = index.add("memo", text, at=run_id,
                          meta={"source": f"run {run_id}"})
        if added:
            index.save()
        return added
    return _safe("memo", run)


def disclosures(signals: dict[str, Any]) -> int:
    """
    Disclosed trades, written as sentences rather than as rows.

    A retrieval index over "AAPL BUY 8000 2026-08-18" retrieves nothing useful
    for a query like "who has been buying". Prose does, because that is what
    the embedder was trained on.
    """
    def run() -> int:
        index = rag.index()
        added = 0
        for symbol, signal in signals.items():
            trades = getattr(signal, "trades", None)
            if trades is None and isinstance(signal, dict):
                trades = signal.get("trades", [])
            for t in (trades or [])[:20]:
                get = (lambda k: t.get(k, "")) if isinstance(t, dict) else (lambda k: getattr(t, k, ""))
                verb = "bought" if get("direction") == "BUY" else "sold"
                sentence = (
                    f"{get('actor')} {verb} {symbol} on {get('traded_on')}, "
                    f"disclosed {get('filed_on')}, about ${float(get('value_usd') or 0):,.0f}. "
                    f"{get('detail')}")
                added += index.add(
                    "disclosure", sentence, symbol=symbol,
                    at=str(get("filed_on")),
                    meta={"source": f"{get('source')} filing"},
                )
        if added:
            index.save()
        return added
    return _safe("disclosures", run)


def committee(symbol: str, verdict: dict[str, Any]) -> int:
    """
    What the committee concluded, so a later sitting can see whether it is
    repeating itself or genuinely changing its mind.
    """
    def run() -> int:
        seats = verdict.get("seats", [])
        lines = [
            f"Committee on {symbol}: {verdict.get('consensus')} at "
            f"{verdict.get('score')}/10, disagreement {verdict.get('disagreement')}."
        ]
        for s in seats:
            if not s.get("answered"):
                continue
            lines.append(
                f"{s.get('name')} said {s.get('stance')} "
                f"(conviction {s.get('conviction')}): "
                f"{'; '.join((s.get('bull') or [])[:2])}. "
                f"Against: {'; '.join((s.get('bear') or [])[:2])}.")
        index = rag.index()
        added = index.add("committee", " ".join(lines), symbol=symbol,
                          at=str(verdict.get("at", "")),
                          meta={"source": "committee"})
        if added:
            index.save()
        return added
    return _safe("committee verdict", run)


def prune(keep: int = 8000) -> int:
    """Called at the end of a run so the index cannot grow without bound."""
    def run() -> int:
        index = rag.index()
        dropped = index.prune(keep)
        if dropped:
            index.save()
        return dropped
    return _safe("prune", run)
