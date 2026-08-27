"""
Kelly sizing for event-driven entries.

Kelly answers one question: given an edge, what fraction of capital maximises
long-run growth. The formula is not the hard part. The hard part is that it
takes a true win probability as input, and here that number comes from a model
reading a headline. A `p` that is wrong by ten points does not cost you ten
points of growth, it can push you past the growth-optimal peak into the region
where more betting means less money.

So three guards, all enforced here rather than left to config:

  1. Fractional Kelly. Quarter Kelly by default. It keeps roughly 44% of the
     growth rate of full Kelly at a quarter of the variance, and it stays on
     the safe side of the peak when p is overestimated.
  2. A hard ceiling at max_single_position. Kelly may size down, never up.
     If the maths says bet 40% and the limit is 25%, you bet 25%.
  3. No estimate, no bet. A missing or nonsensical probability returns a
     refusal with a reason, not a default guess.

Kelly is used for entries only. Exits are risk actions: a broken thesis closes
whether or not the arithmetic likes the odds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config


@dataclass(frozen=True)
class Sizing:
    """The outcome of a sizing attempt. `weight` is a fraction of equity."""
    ok: bool
    weight: float
    reason: str
    full_kelly: float = 0.0
    fractional_kelly: float = 0.0
    capped_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "weight": round(self.weight, 6),
            "reason": self.reason,
            "full_kelly": round(self.full_kelly, 6),
            "fractional_kelly": round(self.fractional_kelly, 6),
            "capped_by": self.capped_by,
        }


def full_kelly(p: float, upside: float, downside: float) -> float:
    """
    f* = (p*b - q) / b, with b the payoff ratio (upside / downside).

    `upside` and `downside` are the expected magnitudes as positive decimals:
    upside 0.08 and downside 0.04 means the case for is +8% and the case
    against is -4%, so b = 2.
    """
    if downside <= 0 or upside <= 0:
        return 0.0
    b = upside / downside
    q = 1.0 - p
    return (p * b - q) / b


def size(
    p: float | None,
    upside: float | None,
    downside: float | None,
    *,
    current_weight: float = 0.0,
    risk_cfg: dict | None = None,
) -> Sizing:
    """
    Turn an edge estimate into a target weight, or refuse and say why.

    Returns the TARGET weight for the position, not the increment, so the
    caller can diff it against what is already held.
    """
    risk_cfg = risk_cfg or config.risk()
    kcfg = risk_cfg.get("kelly", {})
    limits = risk_cfg["limits"]

    fraction = float(kcfg.get("fraction", 0.25))
    min_pos = float(kcfg.get("min_position_pct", 0.02))
    max_single = float(limits["max_single_position"])

    if kcfg.get("require_explicit_estimate", True):
        if p is None or upside is None or downside is None:
            return Sizing(False, 0.0,
                          "no probability or payoff estimate, so there is no "
                          "Kelly fraction to compute")

    p = float(p or 0.0)
    upside = float(upside or 0.0)
    downside = float(downside or 0.0)

    if not 0.0 < p < 1.0:
        return Sizing(False, 0.0, f"probability {p} is not strictly between 0 and 1")
    if upside <= 0 or downside <= 0:
        return Sizing(False, 0.0, "upside and downside must both be positive magnitudes")
    if downside >= 1.0:
        return Sizing(False, 0.0, "a downside of 100% or more cannot be Kelly sized")

    raw = full_kelly(p, upside, downside)
    if raw <= 0:
        return Sizing(False, 0.0,
                      f"no edge: full Kelly is {raw:.3f}, the odds do not favour the bet",
                      full_kelly=raw)

    frac = raw * fraction
    weight = frac
    capped_by = ""

    # Guard 2. Kelly may size down from the position cap, never above it.
    if weight > max_single:
        weight = max_single
        capped_by = "max_single_position"

    # Never propose growing past the cap when something is already held.
    headroom = max(0.0, max_single - current_weight)
    if weight - current_weight > headroom:
        weight = current_weight + headroom
        capped_by = capped_by or "max_single_position"

    if weight < min_pos:
        return Sizing(
            False, round(weight, 6),
            f"quarter Kelly sizes this at {weight:.1%}, below the {min_pos:.0%} "
            f"floor where the edge stops being worth the spread",
            full_kelly=raw, fractional_kelly=frac,
        )

    return Sizing(
        True, round(weight, 6),
        f"full Kelly {raw:.1%}, at {fraction:g}x gives {frac:.1%}"
        + (f", capped to {weight:.1%} by {capped_by}" if capped_by else ""),
        full_kelly=raw, fractional_kelly=frac, capped_by=capped_by,
    )


def describe(sizing: Sizing, symbol: str) -> str:
    """One line for the memo and the dashboard."""
    if not sizing.ok:
        return f"{symbol}: no Kelly size. {sizing.reason}."
    return f"{symbol}: target {sizing.weight:.1%} of equity. {sizing.reason}."
