"""
Are we actually beating the thing we are trying to beat?

The stated aim is 8 to 10 points a year over SPY. This module does not try to
produce that; it measures whether it happened and says so plainly, including
when the answer is no.

Three guards keep the number honest, because an edge figure is the easiest
thing on a dashboard to accidentally flatter:

  It refuses to annualise a short sample. Scaling six weeks of returns to a
  yearly rate produces a headline number that is mostly noise with a large
  multiplier on it, and the direction of that noise decides whether the product
  looks like a triumph or a disaster.

  It always reports the do-nothing comparison beside the result. The relevant
  question is never "did the portfolio go up", it is "did any of this beat
  putting the same money in the benchmark and going away".

  It reports the verdict against the target and the verdict against the
  benchmark separately. Missing an ambitious target while still beating the
  index is a good outcome described badly, and conflating the two would hide
  that.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, asdict
from typing import Any, Sequence

from . import config

TRADING_DAYS = 252


@dataclass
class Edge:
    benchmark: str
    sessions: int
    portfolio_return_pct: float
    benchmark_return_pct: float
    excess_pct: float

    annualised: bool
    portfolio_annual_pct: float | None
    benchmark_annual_pct: float | None
    excess_annual_pct: float | None

    target_pct: float
    stretch_pct: float
    alarm_pct: float

    verdict: str                 # on target | ahead of benchmark | behind | provisional
    beating_benchmark: bool
    headline: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _annualise(total_pct: float, sessions: int) -> float:
    growth = 1.0 + total_pct / 100.0
    if growth <= 0:
        return -100.0
    years = sessions / TRADING_DAYS
    if years <= 0:
        return 0.0
    return (growth ** (1.0 / years) - 1.0) * 100.0


def evaluate(points: Sequence[dict[str, Any]],
             benchmark_points: Sequence[dict[str, Any]] | None = None,
             *, cfg: dict[str, Any] | None = None) -> Edge | None:
    """
    `points` and `benchmark_points` are equity curves, each a list of dicts
    with an `equity` key, aligned on the same dates. Returns None when there is
    not enough of a curve to say anything at all.
    """
    cfg = cfg or config.risk().get("objective", {})
    bench_symbol = str(cfg.get("benchmark", "SPY"))
    target = float(cfg.get("target_excess_annual_pct", 8.0))
    stretch = float(cfg.get("stretch_excess_annual_pct", 10.0))
    alarm = float(cfg.get("alarm_excess_annual_pct", -2.0))
    minimum = int(cfg.get("min_sessions_for_verdict", 120))

    series = [float(p["equity"]) for p in points if p.get("equity")]
    if len(series) < 2 or series[0] <= 0:
        return None

    sessions = len(series)
    port_total = (series[-1] / series[0] - 1) * 100

    bench_total = 0.0
    if benchmark_points:
        bench = [float(p["equity"]) for p in benchmark_points if p.get("equity")]
        if len(bench) >= 2 and bench[0] > 0:
            bench_total = (bench[-1] / bench[0] - 1) * 100

    excess = port_total - bench_total
    beating = excess > 0

    # Annualising anything shorter than the configured minimum turns a noisy
    # sample into a confident yearly rate, so below that the figures stay as
    # measured and the verdict says the sample is too short.
    annualised = sessions >= minimum
    port_annual = _annualise(port_total, sessions) if annualised else None
    bench_annual = _annualise(bench_total, sessions) if annualised else None
    excess_annual = (round(port_annual - bench_annual, 2)
                     if annualised and port_annual is not None
                     and bench_annual is not None else None)

    if not annualised:
        verdict = "provisional"
        headline = (f"{sessions} sessions is too short to call. Ahead of "
                    f"{bench_symbol} by {excess:+.1f} points so far."
                    if beating else
                    f"{sessions} sessions is too short to call. Behind "
                    f"{bench_symbol} by {excess:+.1f} points so far.")
        detail = (f"An edge is not reported as an annual rate until "
                  f"{minimum} sessions have been recorded. Scaling a sample "
                  f"this short to a yearly figure would mostly be amplifying "
                  f"noise.")
    else:
        value = excess_annual if excess_annual is not None else 0.0
        if value >= stretch:
            verdict = "ahead of stretch"
            headline = f"{value:+.1f} points a year over {bench_symbol}, past the {stretch:.0f} point stretch."
        elif value >= target:
            verdict = "on target"
            headline = f"{value:+.1f} points a year over {bench_symbol}, at or above the {target:.0f} point target."
        elif value > 0:
            verdict = "ahead of benchmark, short of target"
            headline = (f"{value:+.1f} points a year over {bench_symbol}. "
                        f"Ahead of the index but short of the {target:.0f} point target.")
        elif value > alarm:
            verdict = "behind benchmark"
            headline = f"{value:+.1f} points a year against {bench_symbol}. The book is losing to the index."
        else:
            verdict = "alarm"
            headline = (f"{value:+.1f} points a year against {bench_symbol}, past the "
                        f"{alarm:.0f} point alarm. Holding the benchmark would have done better.")
        detail = (f"Portfolio {port_annual:+.1f}% a year against {bench_symbol} at "
                  f"{bench_annual:+.1f}%, over {sessions} sessions. "
                  f"The comparison is the same money in {bench_symbol} across "
                  f"identical dates, before costs on both sides.")

    return Edge(
        benchmark=bench_symbol, sessions=sessions,
        portfolio_return_pct=round(port_total, 2),
        benchmark_return_pct=round(bench_total, 2),
        excess_pct=round(excess, 2),
        annualised=annualised,
        portfolio_annual_pct=round(port_annual, 2) if port_annual is not None else None,
        benchmark_annual_pct=round(bench_annual, 2) if bench_annual is not None else None,
        excess_annual_pct=excess_annual,
        target_pct=target, stretch_pct=stretch, alarm_pct=alarm,
        verdict=verdict, beating_benchmark=beating,
        headline=headline, detail=detail,
    )
