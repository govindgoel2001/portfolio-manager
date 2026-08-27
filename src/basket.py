"""
The recommended basket: what to actually hold, in plain words.

Everything else in this system produces analysis. This produces an answer: a
diversified spread across asset classes, each holding with one sentence saying
why it is there, and an honest paragraph about what to expect from it.

Two things it deliberately does not do.

It does not quote a Sharpe ratio, an information coefficient or a beta at the
person reading it. Those numbers are real and they are computed elsewhere in
this codebase, but a beginner asked what to buy is not helped by a statistic
they would have to look up. The reasoning is in sentences.

It does not promise the target. The structure here is core and satellite, and
the arithmetic of that is worth stating plainly: the core keeps you near the
market and therefore cannot beat it, so any edge has to come out of the
satellite sleeves, which is also where any shortfall comes from. A basket
built to be a safe hold and a basket built to beat the index by eight points
are pulling in opposite directions, and this module reports where it landed
between them rather than claiming both.

Following somebody else's portfolio is supported and is treated the same way:
their holdings enter as candidates, the rubric and the risk limits still apply,
and a name they hold that this system would not touch does not get in on their
reputation.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, asdict
from typing import Any

from . import config

log = logging.getLogger(__name__)


@dataclass
class Pick:
    symbol: str
    sleeve: str
    weight: float
    score: float | None
    why: str
    backers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Sleeve:
    key: str
    label: str
    plain: str
    target: float
    actual: float
    picks: list[Pick] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["picks"] = [p.to_dict() for p in self.picks]
        return d


@dataclass
class Basket:
    generated: str
    mode: str                      # balanced | follow
    following: str | None
    sleeves: list[Sleeve]
    cash: float
    invested: float
    headline: str
    expectations: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sleeves"] = [s.to_dict() for s in self.sleeves]
        return d

    @property
    def picks(self) -> list[Pick]:
        return [p for s in self.sleeves for p in s.picks]


# --------------------------------------------------------------------------
# plain language
# --------------------------------------------------------------------------

def _why(symbol: str, score: Any, sleeve_key: str, backers: list[str]) -> str:
    """
    One sentence, no jargon, and only claims the data supports.

    Deliberately vague where the data is vague. "Scores well on what it earns
    against what it costs" is honest about a composite; naming a precise
    multiple this module has not checked would not be.
    """
    # Funds that are the benchmark, or near enough. Owning one is a legitimate
    # core holding and a slightly strange thing to find in a portfolio built to
    # beat that benchmark, so it says so rather than sitting there looking like
    # a stock pick.
    bench = str(config.universe().get("benchmark", "")).upper()
    same_thing = {bench, "VOO", "VTI", "SPY"}
    if symbol.upper() in same_thing:
        return (f"{symbol} is essentially the market itself. Owning it means "
                f"matching the index on this slice and never beating it, which "
                f"is the trade: it is the least surprising thing in the basket "
                f"and the least likely to go badly wrong.")

    if symbol.upper() in {"QQQ", "IWM", "DIA"}:
        which = {"QQQ": "the hundred largest companies on the Nasdaq",
                 "IWM": "two thousand smaller US companies",
                 "DIA": "the thirty companies in the Dow"}[symbol.upper()]
        return (f"{symbol} owns {which} in one holding, so a bad year for any "
                f"single company barely registers. It moves with the market "
                f"rather than against it.")

    bits: list[str] = []

    components = getattr(score, "components", None) or {}
    unscored = set(getattr(score, "unscored", None) or [])

    def measured(name: str) -> float | None:
        return components.get(name) if name not in unscored else None

    value, quality = measured("valuation"), measured("quality")
    trend, momentum = measured("trend"), measured("momentum")

    if value is not None and value >= 65:
        bits.append("looks reasonably priced for what it earns")
    elif value is not None and value <= 35:
        bits.append("is not cheap, and is here for other reasons")
    if quality is not None and quality >= 65:
        bits.append("runs a profitable, financially solid business")
    if trend is not None and trend >= 65:
        bits.append("has been trending up rather than drifting down")
    elif momentum is not None and momentum >= 70:
        bits.append("has been one of the stronger performers lately")

    # ETFs have no margins or earnings to read, so the rubric can only speak
    # to price. Saying what the fund actually holds is more use to a beginner
    # than another sentence about trend.
    themes = {
        "SMH": "owns the semiconductor makers, the picks and shovels of the AI build out",
        "SOXX": "owns the semiconductor industry in one holding",
        "VGT": "owns the technology sector broadly, at a low fee",
        "VUG": "owns fast growing large companies rather than cheap ones",
        "VYM": "owns companies that pay steady dividends, which tend to fall less hard",
        "ARKK": "is a concentrated bet on speculative innovation, and is the most volatile thing here",
        "ICLN": "owns clean energy companies worldwide",
        "TAN": "owns solar companies, which are volatile and cyclical",
        "GLD": "holds physical gold",
        "IAU": "holds physical gold at a slightly lower fee than GLD",
        "GDX": "owns gold miners, which move more than gold itself in both directions",
        "USO": "tracks the price of oil rather than oil companies",
        "BNO": "tracks Brent crude rather than US oil",
        "XOP": "owns oil and gas explorers, the most volatile end of energy",
    }
    if symbol.upper() in themes:
        bits.insert(0, themes[symbol.upper()])

    if sleeve_key == "sector_tilt":
        bits.insert(0, "holds an entire sector rather than one company")
    elif sleeve_key == "hard_assets":
        bits.insert(0, "tends to move differently from shares")
    elif sleeve_key == "crypto":
        bits.insert(0, "is the speculative corner, kept small on purpose")

    if backers:
        who = ", ".join(backers[:2])
        bits.append(f"is also held by {who}, who have beaten the index on our measurement")

    if not bits:
        return (f"{symbol} clears the basic screen but nothing about it stands "
                f"out, so it is here as a diversifier rather than a conviction.")

    body = bits[0] + ("" if len(bits) == 1 else
                      (", and " + bits[1] if len(bits) == 2 else
                       ", " + ", ".join(bits[1:-1]) + ", and " + bits[-1]))
    return f"{symbol} {body}."


def _expectations(invested: float, satellite: float, cash: float) -> list[str]:
    """
    What a person should actually expect. Written to disappoint slightly
    rather than to sell, because the alternative is a user who is surprised in
    the wrong direction.
    """
    cfg = config.risk().get("objective", {})
    target = float(cfg.get("target_excess_annual_pct", 8.0))

    return [
        (f"About {invested * 100:.0f}% of the money is invested and "
         f"{cash * 100:.0f}% is held back. The cash is not idle: it is what lets "
         f"the portfolio buy during a fall instead of selling to raise money."),

        (f"Roughly {satellite * 100:.0f}% sits in the sector, commodity and "
         f"crypto sleeves. That is the only part that can beat the market, "
         f"because the core is built to track it. It is also the only part "
         f"that can lose to the market."),

        (f"The stated aim is {target:.0f} points a year over the S&P. Be "
         f"skeptical of it. Most professional managers do not manage it, and "
         f"the leaderboard in this app measures that rather than asserting it: "
         f"most of the famous names on it are behind the index."),

        ("This is a hold, not a trade. Positions have a minimum holding period "
         "and the system will refuse to churn them. If you are checking it "
         "daily hoping for action, the design is working and you are not."),
    ]


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------

def _eligible(scores: dict[str, Any], classes: list[str],
              min_score: float) -> list[tuple[str, Any]]:
    rows = []
    for symbol, score in scores.items():
        if not getattr(score, "eligible", False):
            continue
        if config.class_of(symbol) not in classes:
            continue
        if (getattr(score, "total", 0) or 0) < min_score:
            continue
        rows.append((symbol, score))
    return rows


def build(scores: dict[str, Any], *, tracker_board: dict[str, Any] | None = None,
          follow: str | None = None) -> Basket:
    """
    `scores` is score.score_universe output. `follow` names a tracker whose
    holdings become the candidate set instead of the whole universe.
    """
    cfg = config.basket()
    limits = config.risk()["limits"]
    min_score = float(cfg.get("min_score", 55))
    cap = float(limits["max_single_position"])
    min_position = float(limits.get("min_position", 0.03))

    backers = _backers(tracker_board) if cfg.get("tracker_tilt", {}).get("enabled") else {}
    warnings: list[str] = []

    followed: set[str] | None = None
    overlap: set[str] = set()
    if follow:
        followed = _followed_symbols(tracker_board, follow)
        if not followed:
            warnings.append(
                f"Could not find current holdings for {follow}, so this is the "
                f"balanced basket instead of a copy of theirs.")
            follow = None
        else:
            # The universe is the hard boundary on what may ever be held, and
            # following somebody is not a reason to widen it. Say plainly how
            # much of their book is actually reachable, because "copying
            # Duquesne" while holding three of their twenty five names is a
            # very different thing from copying Duquesne.
            universe = {s.upper() for s in config.all_symbols()}
            overlap = {s for s in followed if s in universe}
            missing = len(followed) - len(overlap)
            if not overlap:
                warnings.append(
                    f"None of {follow}'s {len(followed)} disclosed holdings are "
                    f"in this portfolio's universe, so there is nothing to copy. "
                    f"Showing the balanced basket instead.")
                followed, follow = None, None
            elif missing:
                warnings.append(
                    f"{follow} discloses {len(followed)} holdings and "
                    f"{len(overlap)} of them are in this portfolio's universe. "
                    f"You are copying {len(overlap)} of their {len(followed)} "
                    f"names, not their portfolio. The rest are outside what this "
                    f"system is allowed to hold.")

    sleeves: list[Sleeve] = []
    invested = 0.0
    cash_floor = float(cfg["cash"]["min"])
    budget = 1.0 - cash_floor

    for key, spec in cfg["sleeves"].items():
        target = float(spec["target"])
        candidates = _eligible(scores, list(spec["classes"]), min_score)

        if followed is not None:
            # Their names, still held to our screen. A name they hold that this
            # system would not touch does not get in on their reputation.
            candidates = [(s, sc) for s, sc in candidates if s in followed]

        ranked = sorted(
            candidates,
            key=lambda r: (r[1].total or 0) + _bonus(r[0], backers, cfg),
            reverse=True)[:int(spec["max_names"])]

        sleeve = Sleeve(key=key, label=spec["label"], plain=str(spec["plain"]).strip(),
                        target=target, actual=0.0)

        if not ranked:
            sleeve.note = (f"Nothing in this sleeve cleared the screen, so it is "
                           f"empty and the money stays in cash rather than being "
                           f"forced into a name that did not qualify.")
            sleeves.append(sleeve)
            continue

        # Split the sleeve evenly. A beginner basket does not benefit from
        # optimised weights inside a sleeve: the differences are inside the
        # error bars of the scores that produced them.
        each = min(target / len(ranked), cap)
        if each < min_position:
            each = min_position
            ranked = ranked[:max(int(target / min_position), 1)]

        for symbol, score in ranked:
            who = backers.get(symbol, [])
            sleeve.picks.append(Pick(
                symbol=symbol, sleeve=key, weight=round(each, 4),
                score=round(score.total, 1) if score.total is not None else None,
                why=_why(symbol, score, key, who), backers=who))

        sleeve.actual = round(sum(p.weight for p in sleeve.picks), 4)
        invested += sleeve.actual
        sleeves.append(sleeve)

    # If the sleeves overshot what may be deployed, scale them back together
    # rather than emptying whichever happened to be built last.
    if invested > budget and invested > 0:
        scale = budget / invested
        for sleeve in sleeves:
            for pick in sleeve.picks:
                pick.weight = round(pick.weight * scale, 4)
            sleeve.actual = round(sum(p.weight for p in sleeve.picks), 4)
        invested = round(sum(s.actual for s in sleeves), 4)
        warnings.append(
            f"Sleeve targets summed above the {budget * 100:.0f}% that may be "
            f"deployed, so every holding was scaled back proportionally.")

    cash = round(1.0 - invested, 4)
    # Satellite is whatever is not flagged core in config. Inferring it by
    # excluding one hard coded sleeve name was wrong the moment a second core
    # sleeve existed, and it quietly overstated the risky share by the size of
    # the index funds.
    core_keys = {k for k, spec in cfg["sleeves"].items() if spec.get("core")}
    satellite = round(sum(s.actual for s in sleeves if s.key not in core_keys), 4)

    empty = [s.label for s in sleeves if not s.picks]
    if empty:
        warnings.append(
            f"Empty this run: {', '.join(empty)}. The basket is less diversified "
            f"than intended until those sleeves have something that qualifies.")

    headline = (f"Copying {follow}, filtered through the same screen"
                if follow else
                f"A diversified hold across {sum(1 for s in sleeves if s.picks)} "
                f"asset groups, {invested * 100:.0f}% invested")

    return Basket(
        generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        mode="follow" if follow else "balanced",
        following=follow,
        sleeves=sleeves, cash=cash, invested=round(invested, 4),
        headline=headline,
        expectations=_expectations(invested, satellite, cash),
        warnings=warnings,
    )


def _bonus(symbol: str, backers: dict[str, list[str]], cfg: dict[str, Any]) -> float:
    tilt = cfg.get("tracker_tilt", {})
    if not tilt.get("enabled"):
        return 0.0
    n = len(backers.get(symbol, []))
    return min(n * float(tilt.get("bonus_per_backer", 2.0)),
               float(tilt.get("max_bonus", 6.0)))


def _backers(board: dict[str, Any] | None) -> dict[str, list[str]]:
    """Which managers with a positive measured record hold each name."""
    out: dict[str, list[str]] = {}
    if not board:
        return out
    for t in board.get("trackers", []):
        if (t.get("mean_excess") or 0) <= 0:
            continue
        if (t.get("stale_days") or 0) > 150:
            continue
        for h in t.get("holdings", []):
            out.setdefault(h["symbol"], []).append(t["name"])
    return out


def _followed_symbols(board: dict[str, Any] | None, name: str) -> set[str]:
    if not board:
        return set()
    needle = name.strip().lower()
    for t in board.get("trackers", []):
        if t["name"].strip().lower() == needle or t.get("key") == name:
            return {h["symbol"] for h in t.get("holdings", [])}
    return set()


def followable(board: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    Portfolios a person can choose to follow, with the number that matters
    next to each: how they did against the index, not how famous they are.
    """
    if not board:
        return []
    rows = []
    for t in board.get("trackers", []):
        if t.get("mean_excess") is None or not t.get("holdings"):
            continue
        rows.append({
            "name": t["name"],
            "key": t.get("key"),
            "kind": t.get("kind"),
            "mean_excess": t["mean_excess"],
            "beat_rate": t.get("beat_rate"),
            "positions": len(t.get("holdings", [])),
            "stale_days": t.get("stale_days"),
            "verdict": ("has beaten the index on our measurement"
                        if t["mean_excess"] > 0 else
                        "has trailed the index on our measurement"),
        })
    rows.sort(key=lambda r: r["mean_excess"], reverse=True)
    return rows
