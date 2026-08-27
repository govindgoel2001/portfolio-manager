"""
Portfolio construction.

A high score is not a buy. This module turns the ranked opportunity set into
target weights under an explicit policy, and it treats "no change" as a
first-class outcome: if nothing crosses the rebalance band, it proposes
nothing and the daily run is a monitoring run.

Allocation deliberately respects the same limits the risk gate enforces, so a
clean run produces a proposal that passes. The gate stays as the backstop that
catches anything this module gets wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from .score import Score

ACTIONS = ("OPEN", "INCREASE", "REDUCE", "EXIT", "KEEP")


@dataclass
class Proposal:
    symbol: str
    asset_class: str
    action: str
    current_weight: float
    target_weight: float
    delta_weight: float
    delta_usd: float
    score: float
    score_components: dict[str, float]
    unscored: list[str]
    # Filled by the LLM pass; the structural gates in risk.py require them.
    thesis: str = ""
    counter: str = ""
    exit_rule: str = ""
    invalidation: str = ""
    confidence: str = "unrated"
    role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "action": self.action,
            "current_weight": round(self.current_weight, 6),
            "target_weight": round(self.target_weight, 6),
            "delta_weight": round(self.delta_weight, 6),
            "delta_usd": round(self.delta_usd, 2),
            "score": self.score,
            "score_components": self.score_components,
            "unscored": self.unscored,
            "thesis": self.thesis,
            "counter": self.counter,
            "exit_rule": self.exit_rule,
            "invalidation": self.invalidation,
            "confidence": self.confidence,
            "role": self.role,
        }


def current_weights(positions: list[Any], equity: float) -> dict[str, float]:
    if equity <= 0:
        return {}
    return {p.symbol.upper(): p.market_value / equity for p in positions}


def build(
    scores: dict[str, Score],
    positions: list[Any],
    equity: float,
    *,
    risk_cfg: dict | None = None,
) -> list[Proposal]:
    risk_cfg = risk_cfg or config.risk()
    limits = risk_cfg["limits"]
    threshold = float(risk_cfg.get("score_threshold", 60))

    max_single = float(limits["max_single_position"])
    max_class = float(limits.get("max_class_exposure", 1.0))
    min_cash = float(limits["min_cash"])
    max_gross = float(limits["max_gross_exposure"])
    min_pos = float(limits.get("min_position", 0.0))
    band = float(limits.get("rebalance_band", 0.03))
    max_new = int(limits.get("max_new_positions_per_run", 999))
    max_turnover = float(limits.get("max_turnover_per_run", 1.0))

    held = current_weights(positions, equity)
    budget = min(max_gross, 1.0 - min_cash)

    eligible = sorted(
        (s for s in scores.values() if s.eligible),
        key=lambda s: s.total, reverse=True,
    )

    targets = _optimal_weights(
        eligible, budget,
        max_single=max_single, max_class=max_class,
        min_pos=min_pos, threshold=threshold,
    )

    # Anything currently held that is no longer eligible goes to zero.
    for sym in held:
        targets.setdefault(sym, 0.0)

    # Cap how many brand-new positions a single run may open, weakest first.
    new_names = [s for s in targets if s not in held and targets[s] > 0]
    if len(new_names) > max_new:
        ranked = sorted(new_names, key=lambda s: scores[s].total if s in scores else 0.0, reverse=True)
        for sym in ranked[max_new:]:
            targets[sym] = 0.0

    targets = _apply_band(targets, held, band, min_pos)
    targets = _fit_turnover(targets, held, max_turnover, scores)

    proposals: list[Proposal] = []
    for sym in sorted(set(targets) | set(held)):
        cur = held.get(sym, 0.0)
        tgt = targets.get(sym, cur)
        delta = tgt - cur
        sc = scores.get(sym)
        proposals.append(Proposal(
            symbol=sym,
            asset_class=(sc.asset_class if sc else config.class_of(sym)),
            action=_action(cur, tgt),
            current_weight=cur,
            target_weight=tgt,
            delta_weight=delta,
            delta_usd=delta * equity,
            score=(sc.total if sc else 0.0),
            score_components=(sc.components if sc else {}),
            unscored=(sc.unscored if sc else ["no score - not in universe"]),
        ))

    # Actionable first, then held names, then the rest.
    return sorted(
        proposals,
        key=lambda p: (p.action == "KEEP", -abs(p.delta_weight), -p.target_weight, p.symbol),
    )


# --------------------------------------------------------------------------

def _optimal_weights(
    eligible: list[Score], budget: float, *,
    max_single: float, max_class: float, min_pos: float, threshold: float,
) -> dict[str, float]:
    """
    Score-proportional weights, then iteratively cap by name and by class,
    redistributing the overflow to names with headroom.
    """
    if not eligible or budget <= 0:
        return {}

    # Only as many names as can each clear the minimum position size, counted
    # per class as well as overall.
    #
    # Weights are score proportional, so each one shrinks as the candidate pool
    # grows. Past a certain width every weight falls under min_pos, the dust
    # filter at the end drops all of them, and this returns an empty dict: the
    # allocator then proposes nothing at all, with no error anywhere.
    #
    # The per-class limit is the one that actually bites. A class capped at 40%
    # of equity with a 3% minimum can hold thirteen names, however wide the
    # budget is, so truncating only on the overall budget still let one class
    # fill up with names too small to survive its own cap.
    #
    # Holding a concentrated book off a broad screen is the intent regardless:
    # the pool is a search space, not a shopping list.
    if min_pos > 0:
        per_class = max(int(max_class / min_pos), 1)
        overall = max(int(budget / min_pos), 1)
        seen: dict[str, int] = {}
        kept: list[Score] = []
        for s in eligible:                      # already sorted best first
            n = seen.get(s.asset_class, 0)
            if n >= per_class or len(kept) >= overall:
                continue
            seen[s.asset_class] = n + 1
            kept.append(s)
        eligible = kept

    # +10 so the lowest eligible name still gets a non-zero share rather than
    # a weight of exactly zero at the threshold.
    strength = {s.symbol: max(s.total - threshold + 10.0, 1.0) for s in eligible}
    total = sum(strength.values())
    weights = {sym: budget * v / total for sym, v in strength.items()}
    classes = {s.symbol: s.asset_class for s in eligible}

    for _ in range(24):
        overflow = 0.0
        # per-name cap
        for sym, w in list(weights.items()):
            if w > max_single:
                overflow += w - max_single
                weights[sym] = max_single
        # per-class cap
        by_class: dict[str, float] = {}
        for sym, w in weights.items():
            by_class[classes[sym]] = by_class.get(classes[sym], 0.0) + w
        for cls, w in by_class.items():
            if w > max_class:
                excess = w - max_class
                overflow += excess
                members = [s for s in weights if classes[s] == cls]
                for sym in members:
                    weights[sym] -= excess * (weights[sym] / w)

        if overflow <= 1e-9:
            break

        headroom = {
            sym: max_single - weights[sym]
            for sym in weights
            if weights[sym] < max_single - 1e-9
            and by_class.get(classes[sym], 0.0) < max_class - 1e-9
        }
        room = sum(headroom.values())
        if room <= 1e-9:
            break  # genuinely cannot deploy the budget; stays in cash
        for sym, h in headroom.items():
            weights[sym] += overflow * (h / room)

    # Drop dust rather than proposing a position too small to matter.
    return {s: round(w, 6) for s, w in weights.items() if w >= min_pos - 1e-9}


def _apply_band(
    targets: dict[str, float], held: dict[str, float], band: float, min_pos: float
) -> dict[str, float]:
    """
    Ignore changes smaller than the rebalance band. Churn control.

    The band governs adjusting a position you already hold, not whether to
    take one. Opening and closing are decisions; nudging a 6% holding to 8%
    is tuning, and tuning is what this suppresses.

    Applying it to entries as well was a real bug: as the universe grew past
    forty names, each optimal weight fell below the band, every entry read as
    drift, and the allocator quietly proposed nothing at all on an empty
    portfolio. A churn control that prevents ever buying anything is not a
    churn control.
    """
    out = dict(targets)
    for sym, tgt in targets.items():
        cur = held.get(sym, 0.0)
        # A full exit is always allowed through; it is a risk action.
        if tgt == 0.0 and cur > 0.0:
            continue
        # Opening from nothing is an entry, not drift. The minimum position
        # size is what stops it being trivially small, and it is already
        # enforced upstream.
        if cur == 0.0 and tgt >= min_pos:
            continue
        if abs(tgt - cur) < band:
            out[sym] = cur
    return out


def _fit_turnover(
    targets: dict[str, float], held: dict[str, float], max_turnover: float,
    scores: dict[str, Score] | None = None,
) -> dict[str, float]:
    """
    Fit the proposed changes inside the per-run turnover budget.

    Exits are charged first and never scaled: half-closing a position you
    decided to leave is the worst of both. But they are not taken all at once
    either. If the exits together exceed the budget, the weakest go first and
    the rest wait for the next run, because handing the risk gate more exits
    than the cap allows gets every one of them blocked and the portfolio keeps
    holding all of them. Partial progress beats a rejected run.

    A single exit larger than the whole budget is always allowed through. A
    position can otherwise become impossible to close, which is a worse
    failure than one busy day of turnover.
    """
    exits = {s: t for s, t in targets.items() if t == 0.0 and held.get(s, 0.0) > 0.0}
    others = {s: t for s, t in targets.items() if s not in exits}

    # Weakest score exits first; unscored names are treated as weakest.
    ranked = sorted(exits, key=lambda s: (scores[s].total if scores and s in scores else -1.0))

    taken: dict[str, float] = {}
    spent = 0.0
    for sym in ranked:
        cost = held[sym]
        if not taken or spent + cost <= max_turnover + 1e-9:
            taken[sym] = 0.0
            spent += cost
        # else: deferred to a later run, and it keeps its current weight below

    deferred = {s: held[s] for s in exits if s not in taken}

    remaining = max_turnover - spent
    if remaining <= 1e-9:
        return {**taken, **deferred,
                **{s: held.get(s, 0.0) for s in others}}

    cost = sum(abs(t - held.get(s, 0.0)) for s, t in others.items())
    if cost <= remaining + 1e-9:
        return {**taken, **deferred, **others}

    scale = remaining / cost
    out = {**taken, **deferred}
    for sym, tgt in others.items():
        cur = held.get(sym, 0.0)
        out[sym] = round(cur + (tgt - cur) * scale, 6)
    return out


def _action(current: float, target: float) -> str:
    if abs(target - current) < 1e-9:
        return "KEEP"
    if current <= 1e-9 and target > 0:
        return "OPEN"
    if target <= 1e-9 and current > 0:
        return "EXIT"
    return "INCREASE" if target > current else "REDUCE"
