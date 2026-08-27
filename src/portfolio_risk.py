"""
Portfolio-level risk and the allocation backtest.

Two things the per-name limits cannot see.

First, correlation. `max_class_exposure` counts gold and oil as separate
sleeves, but GLD and GDX are two reads on the same gold price, and XLE moves
with crude. A book that passes every class limit can still be one bet wearing
four names. Effective N, computed from the correlation matrix, says how many
independent positions you actually hold.

Second, what the current allocation has historically done. On a new account
the realised equity curve is a flat line at the starting balance, which is
true and useless. Holding today's weights across the trailing window answers
the question an investor actually has: is this allocation any good, and does
it beat owning the benchmark and doing nothing.

That second number is a look back on today's weights, not a record of returns
anyone earned. Every caller labels it as such.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, asdict, field
from typing import Any, Sequence

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# returns plumbing
# --------------------------------------------------------------------------

def daily_returns(bars: Sequence[Any]) -> dict[str, float]:
    """Date to simple return. Bars may be dicts or Bar objects."""
    out: dict[str, float] = {}
    prev = None
    for b in bars:
        close = float(getattr(b, "close", None) or b["close"])
        raw_ts = getattr(b, "ts", None) or b.get("t")
        day = (raw_ts.date().isoformat() if hasattr(raw_ts, "date")
               else str(raw_ts)[:10])
        if prev is not None and prev > 0:
            out[day] = close / prev - 1.0
        prev = close
    return out


def aligned(returns_by_symbol: dict[str, dict[str, float]]) -> tuple[list[str], dict[str, list[float]]]:
    """Restrict every series to the dates all of them share."""
    if not returns_by_symbol:
        return [], {}
    common = set.intersection(*(set(r) for r in returns_by_symbol.values())) \
        if len(returns_by_symbol) > 1 else set(next(iter(returns_by_symbol.values())))
    days = sorted(common)
    return days, {s: [r[d] for d in days] for s, r in returns_by_symbol.items()}


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = list(a[:n]), list(b[:n])
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return max(-1.0, min(1.0, cov / math.sqrt(va * vb)))


# --------------------------------------------------------------------------
# concentration
# --------------------------------------------------------------------------

@dataclass
class Concentration:
    weights: dict[str, float]
    matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    effective_n: float = 0.0
    nominal_n: int = 0
    herfindahl: float = 0.0
    largest_cluster: list[str] = field(default_factory=list)
    largest_cluster_weight: float = 0.0
    portfolio_vol: float = 0.0
    diversification_ratio: float = 1.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyse(
    weights: dict[str, float],
    bars_by_symbol: dict[str, list[Any]],
    *,
    cluster_threshold: float = 0.7,
) -> Concentration:
    """
    Effective N is 1 / sum(w_i w_j rho_ij). With uncorrelated equal positions
    it equals the number of holdings. As correlations rise toward 1 it falls
    toward 1, which is the honest count of how many bets are really on.
    """
    held = {s: w for s, w in weights.items() if w > 1e-9}
    c = Concentration(weights=held, nominal_n=len(held))
    if not held:
        c.note = "no positions"
        return c

    c.herfindahl = round(sum(w * w for w in held.values()), 6)

    rets = {s: daily_returns(bars_by_symbol.get(s, [])) for s in held}
    rets = {s: r for s, r in rets.items() if len(r) >= 30}
    if len(rets) < 2:
        c.effective_n = float(len(held))
        c.note = "not enough overlapping history to measure correlation"
        return c

    days, series = aligned(rets)
    if len(days) < 30:
        c.effective_n = float(len(held))
        c.note = f"only {len(days)} overlapping days, correlation not measured"
        return c

    syms = sorted(series)
    c.matrix = {
        a: {b: round(correlation(series[a], series[b]), 4) for b in syms}
        for a in syms
    }

    # Renormalise across the names we could actually measure.
    total = sum(held[s] for s in syms)
    w = {s: held[s] / total for s in syms} if total else {s: 0.0 for s in syms}

    denom = sum(w[a] * w[b] * c.matrix[a][b] for a in syms for b in syms)
    c.effective_n = round(1.0 / denom, 2) if denom > 0 else float(len(syms))

    vols = {s: statistics.stdev(series[s]) * math.sqrt(TRADING_DAYS)
            for s in syms if len(series[s]) > 1}
    if vols:
        port_var = sum(w[a] * w[b] * c.matrix[a][b] * vols.get(a, 0) * vols.get(b, 0)
                       for a in syms for b in syms)
        c.portfolio_vol = round(math.sqrt(max(port_var, 0.0)), 4)
        weighted_vol = sum(w[s] * vols.get(s, 0.0) for s in syms)
        if c.portfolio_vol > 0:
            c.diversification_ratio = round(weighted_vol / c.portfolio_vol, 3)

    # The largest tightly correlated cluster, and what it weighs.
    best: list[str] = []
    for anchor in syms:
        cluster = [s for s in syms if c.matrix[anchor][s] >= cluster_threshold]
        if sum(held[s] for s in cluster) > sum(held[s] for s in best):
            best = cluster
    c.largest_cluster = sorted(best)
    c.largest_cluster_weight = round(sum(held[s] for s in best), 6)

    if c.nominal_n and c.effective_n < c.nominal_n * 0.6:
        c.note = (
            f"{c.nominal_n} positions behave like {c.effective_n:.1f} independent "
            f"bets. {', '.join(c.largest_cluster)} move together and are "
            f"{c.largest_cluster_weight:.0%} of the book."
        )
    else:
        c.note = f"{c.nominal_n} positions, roughly {c.effective_n:.1f} independent bets."
    return c


# --------------------------------------------------------------------------
# backtest of the current allocation
# --------------------------------------------------------------------------

@dataclass
class Backtest:
    days: list[str]
    portfolio: list[float]
    benchmark: list[float]
    total_return: float = 0.0
    benchmark_return: float = 0.0
    excess: float = 0.0
    max_drawdown: float = 0.0
    benchmark_max_drawdown: float = 0.0
    volatility: float = 0.0
    sharpe: float = 0.0
    beat_benchmark: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def backtest_weights(
    weights: dict[str, float],
    bars_by_symbol: dict[str, list[Any]],
    benchmark: str,
    *,
    starting_equity: float = 10_000.0,
    cash_weight: float | None = None,
) -> Backtest | None:
    """
    Hold these weights, rebalanced never, across the overlapping window, and
    compare against holding the benchmark.

    This is a look back on today's allocation. It is not a record of realised
    returns, it does not model costs or slippage, and it benefits from knowing
    which names are in the book today. Treated as a sanity check on the
    allocation, not as a track record.
    """
    held = {s: w for s, w in weights.items() if w > 1e-9}
    bench_bars = bars_by_symbol.get(benchmark.upper()) or []
    if not held or not bench_bars:
        return None

    rets = {s: daily_returns(bars_by_symbol.get(s, [])) for s in held}
    rets = {s: r for s, r in rets.items() if r}
    bench_rets = daily_returns(bench_bars)
    if not rets or not bench_rets:
        return None

    common = set.intersection(*(set(r) for r in rets.values())) & set(bench_rets)
    days = sorted(common)
    if len(days) < 20:
        return None

    invested = sum(held.values())
    cash_w = cash_weight if cash_weight is not None else max(0.0, 1.0 - invested)

    port_equity, bench_equity = [starting_equity], [starting_equity]
    for d in days:
        # Cash earns nothing here. Assuming a rate would flatter the portfolio
        # against a fully invested benchmark.
        day_return = sum(held[s] * rets[s][d] for s in held) + cash_w * 0.0
        port_equity.append(port_equity[-1] * (1 + day_return))
        bench_equity.append(bench_equity[-1] * (1 + bench_rets[d]))

    labels = [days[0]] + days
    bt = Backtest(days=labels,
                  portfolio=[round(v, 2) for v in port_equity],
                  benchmark=[round(v, 2) for v in bench_equity])

    bt.total_return = round(port_equity[-1] / port_equity[0] - 1.0, 6)
    bt.benchmark_return = round(bench_equity[-1] / bench_equity[0] - 1.0, 6)
    bt.excess = round(bt.total_return - bt.benchmark_return, 6)
    bt.beat_benchmark = bt.excess > 0
    bt.max_drawdown = _max_drawdown(port_equity)
    bt.benchmark_max_drawdown = _max_drawdown(bench_equity)

    daily = [port_equity[i] / port_equity[i - 1] - 1.0
             for i in range(1, len(port_equity))]
    if len(daily) > 2:
        vol = statistics.stdev(daily) * math.sqrt(TRADING_DAYS)
        bt.volatility = round(vol, 4)
        mean_annual = statistics.fmean(daily) * TRADING_DAYS
        bt.sharpe = round(mean_annual / vol, 3) if vol > 0 else 0.0

    verdict = "ahead of" if bt.excess > 0 else "behind"
    bt.note = (
        f"Current weights held across {len(days)} sessions return "
        f"{bt.total_return:+.1%} against {benchmark.upper()} at "
        f"{bt.benchmark_return:+.1%}, {verdict} by {abs(bt.excess):.1%}. "
        f"Worst drawdown {bt.max_drawdown:.1%} against the benchmark's "
        f"{bt.benchmark_max_drawdown:.1%}. This is a look back on today's "
        f"allocation, not returns anyone earned, and it ignores costs."
    )
    return bt


def _max_drawdown(equity: Sequence[float]) -> float:
    peak, worst = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return round(worst, 6)
