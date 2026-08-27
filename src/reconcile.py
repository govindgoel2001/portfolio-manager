"""
What you already own, against what this thing would suggest.

Importing a portfolio is only interesting if something happens next. What
happens here is a comparison: every holding is put beside the recommended
basket and sorted into keep, trim, add, or not covered, with a sentence saying
why in each case.

Two judgements are deliberately conservative.

A holding the basket does not contain is not "sell". It is "not covered", and
the difference matters: this system screens a few dozen instruments, so most
of what a real person owns will be outside it, and calling that a sell signal
would be a system mistaking the edge of its own knowledge for a view about the
asset. Only a name inside the universe that failed the screen gets called out
as one the basket rejected.

Nothing here is an order. It is a comparison, and acting on it goes through
the same approval queue as everything else.

Text import accepts what people actually paste: a broker's CSV, two columns
from a spreadsheet, or a line per holding typed by hand.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from . import config

log = logging.getLogger(__name__)

KEEP, TRIM, ADD, UNCOVERED, REJECTED = "keep", "trim", "add", "uncovered", "rejected"


@dataclass
class Line:
    symbol: str
    verdict: str
    held_weight: float
    target_weight: float
    qty: float = 0.0
    market_value: float = 0.0
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    lines: list[Line]
    total_value: float
    covered_value: float
    headline: str
    notes: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["lines"] = [x.to_dict() for x in self.lines]
        return d


# --------------------------------------------------------------------------
# parsing what people paste
# --------------------------------------------------------------------------

_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-/]{0,9}$")
_NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")


def parse(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Pull holdings out of pasted text. Returns (rows, unparsed lines).

    Deliberately forgiving about shape and strict about ambiguity: a line it
    cannot read with confidence is returned as unparsed and shown back, rather
    than being guessed at. A wrong quantity is worse than a missing one.
    """
    rows: list[dict[str, Any]] = []
    unparsed: list[str] = []
    if not text or not text.strip():
        return rows, unparsed

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Split on commas, tabs, or runs of spaces, whichever the paste used.
        parts = next(
            (p for p in (
                [c.strip() for c in next(csv.reader([line]))] if "," in line else [],
                [c.strip() for c in line.split("\t")] if "\t" in line else [],
                line.split(),
            ) if len(p) >= 2), [])

        if len(parts) < 2:
            unparsed.append(line)
            continue

        symbol = parts[0].upper().strip('"')
        if not _SYMBOL.match(symbol) or symbol.lower() in ("symbol", "ticker"):
            unparsed.append(line)
            continue

        numbers = [p.replace(",", "").replace("$", "").strip()
                   for p in parts[1:]]
        numbers = [n for n in numbers if _NUMBER.match(n)]
        if not numbers:
            unparsed.append(line)
            continue

        qty = float(numbers[0])
        value = float(numbers[1]) if len(numbers) > 1 else 0.0
        rows.append({"symbol": symbol, "qty": qty, "market_value": value})

    return rows, unparsed


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------

def build(positions: list[dict[str, Any]], basket: Any,
          scores: dict[str, Any] | None = None,
          *, prices: dict[str, float] | None = None) -> Report:
    """
    `positions` are dicts with symbol, qty and optionally market_value.
    `basket` is a basket.Basket. `scores` is score_universe output, used only
    to explain why something inside the universe was left out.
    """
    prices = prices or {}
    universe = {s.upper() for s in config.all_symbols()}
    targets = {p.symbol.upper(): p.weight for p in basket.picks}
    reasons = {p.symbol.upper(): p.why for p in basket.picks}

    priced: list[dict[str, Any]] = []
    for row in positions:
        symbol = str(row.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        qty = float(row.get("qty") or 0)
        value = float(row.get("market_value") or 0)
        if not value and prices.get(symbol):
            value = qty * prices[symbol]
        priced.append({"symbol": symbol, "qty": qty, "market_value": value})

    total = sum(p["market_value"] for p in priced)
    notes: list[str] = []
    if total <= 0:
        notes.append(
            "No market values were supplied, so weights cannot be computed and "
            "everything is compared by presence rather than by size. Paste a "
            "value per holding, or connect the broker, to get sizes.")

    lines: list[Line] = []
    seen: set[str] = set()

    for p in priced:
        symbol = p["symbol"]
        seen.add(symbol)
        held_w = (p["market_value"] / total) if total > 0 else 0.0
        target_w = targets.get(symbol, 0.0)

        if symbol in targets:
            # A tolerance, because trimming a holding for two points of drift
            # is churn, and churn is what this system is built not to do.
            if held_w > target_w * 1.5 and held_w - target_w > 0.05:
                verdict, why = TRIM, (
                    f"You hold about {held_w:.0%} of your portfolio in "
                    f"{symbol}; the basket would hold {target_w:.0%}. Bigger "
                    f"positions are not wrong, they are just less diversified.")
            else:
                verdict, why = KEEP, (
                    f"The basket holds this too, at about {target_w:.0%}. "
                    + reasons.get(symbol, ""))
        elif symbol in universe:
            score = (scores or {}).get(symbol)
            eligible = bool(getattr(score, "eligible", False))
            total_score = getattr(score, "total", None)
            verdict = REJECTED
            if not eligible:
                why = (f"{symbol} is one this system follows, and it did not "
                       f"clear the screen this time, so the basket left it out. "
                       f"That is not a sell instruction.")
            else:
                why = (f"{symbol} cleared the screen"
                       + (f" at {total_score:.0f}" if total_score else "")
                       + " but was not among the highest scoring in its group, "
                         "so the basket held something else instead.")
        else:
            verdict = UNCOVERED
            why = (f"{symbol} is outside the few dozen instruments this system "
                   f"follows, so it has no opinion on it either way. Silence "
                   f"here means no information, not a verdict.")

        lines.append(Line(symbol=symbol, verdict=verdict, held_weight=round(held_w, 4),
                          target_weight=round(target_w, 4), qty=p["qty"],
                          market_value=round(p["market_value"], 2), why=why))

    for symbol, weight in targets.items():
        if symbol in seen:
            continue
        lines.append(Line(
            symbol=symbol, verdict=ADD, held_weight=0.0,
            target_weight=round(weight, 4),
            why=(f"The basket would hold about {weight:.0%} here and you hold "
                 f"none. " + reasons.get(symbol, ""))))

    # What they hold comes before what they do not. An import answers
    # "is what I own right" first; a list headed by sixteen names they
    # have never owned buries their actual portfolio below the fold.
    order = {TRIM: 0, REJECTED: 1, KEEP: 2, UNCOVERED: 3, ADD: 4}
    lines.sort(key=lambda x: (order.get(x.verdict, 9), -x.held_weight, x.symbol))

    covered = sum(x.market_value for x in lines if x.verdict in (KEEP, TRIM))
    counts = {v: sum(1 for x in lines if x.verdict == v) for v in order}

    if total > 0:
        share = covered / total
        headline = (f"{counts[KEEP]} of your holdings match the basket, "
                    f"{counts[TRIM]} look oversized, {counts[ADD]} are missing, "
                    f"and {share:.0%} of your money is in something this "
                    f"system has a view on.")
    else:
        headline = (f"{counts[KEEP]} match the basket, {counts[ADD]} are "
                    f"missing, {counts[UNCOVERED]} are outside what this "
                    f"system follows.")

    if counts[UNCOVERED]:
        notes.append(
            f"{counts[UNCOVERED]} holdings are outside the universe. They are "
            f"listed but not judged. Widening coverage means adding them to "
            f"config/universe.yaml, which is a deliberate decision rather than "
            f"something an import should do on its own.")

    return Report(lines=lines, total_value=round(total, 2),
                  covered_value=round(covered, 2), headline=headline,
                  notes=notes)
