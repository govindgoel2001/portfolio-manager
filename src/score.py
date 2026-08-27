"""
The locked scoring rubric.

Every number here is computed in Python from the frozen snapshot. The model is
never asked to do arithmetic - it is asked to explain the arithmetic this
module produced. That is what makes a replay test meaningful: same snapshot in,
byte-identical scores out.

Sub-scores are all 0-100. Missing inputs produce the configured neutral value
and set an `unscored` flag, which the report surfaces rather than hides.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Sequence

from . import config
from . import sentiment as sent
from .data import fundamentals as fu
from .data import smartmoney as sm

TRADING_DAYS = 252


@dataclass
class Features:
    """Raw computed inputs, kept alongside the scores for the audit appendix."""
    last_price: float = 0.0
    returns: dict[str, float] = field(default_factory=dict)
    annual_vol: float = 0.0
    ma_fast: float = 0.0
    ma_slow: float = 0.0
    ma_slow_slope: float = 0.0
    median_dollar_volume: float = 0.0
    bars_available: int = 0
    drawdown_from_high: float = 0.0


@dataclass
class Score:
    symbol: str
    asset_class: str
    total: float
    components: dict[str, float]
    unscored: list[str]
    features: Features
    eligible: bool
    reason: str = ""
    #: share of rubric weight actually measured rather than filled in
    coverage: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["features"] = asdict(self.features)
        return d


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _pct_return(closes: Sequence[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    past = closes[-1 - lookback]
    return (closes[-1] / past - 1.0) if past else None


def _sma(closes: Sequence[float], window: int) -> float | None:
    return statistics.fmean(closes[-window:]) if len(closes) >= window else None


def _annual_vol(closes: Sequence[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _percentile_rank(value: float, population: Sequence[float]) -> float:
    """0-100 rank of value within population. Ties share the midpoint."""
    if not population:
        return 50.0
    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return 100.0 * (below + 0.5 * equal) / len(population)


# --------------------------------------------------------------------------
# feature extraction
# --------------------------------------------------------------------------

def extract_features(bars: list[dict[str, Any]] | list[Any], cfg: dict | None = None) -> Features:
    cfg = cfg or config.scoring()
    closes = [float(getattr(b, "close", None) or b["close"]) for b in bars]
    volumes = [float(getattr(b, "volume", None) or b["volume"]) for b in bars]
    f = Features(bars_available=len(closes))
    if not closes:
        return f

    f.last_price = closes[-1]

    for lb in cfg["momentum"]["lookbacks"]:
        r = _pct_return(closes, lb)
        if r is not None:
            f.returns[f"r{lb}"] = round(r, 6)

    vol = _annual_vol(closes, cfg["volatility"]["window"])
    f.annual_vol = round(vol, 6) if vol is not None else 0.0

    fast = _sma(closes, cfg["trend"]["fast_ma"])
    slow = _sma(closes, cfg["trend"]["slow_ma"])
    f.ma_fast = round(fast, 4) if fast else 0.0
    f.ma_slow = round(slow, 4) if slow else 0.0

    slow_window = cfg["trend"]["slow_ma"]
    prior_slow = _sma(closes[:-10], slow_window) if len(closes) > slow_window + 10 else None
    if slow and prior_slow:
        f.ma_slow_slope = round(slow / prior_slow - 1.0, 6)

    lw = cfg["liquidity"]["window"]
    if len(closes) >= lw:
        dollar = [c * v for c, v in zip(closes[-lw:], volumes[-lw:])]
        f.median_dollar_volume = round(statistics.median(dollar), 2)

    high = max(closes)
    f.drawdown_from_high = round(closes[-1] / high - 1.0, 6) if high else 0.0
    return f


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_universe(
    bars_by_symbol: dict[str, list[Any]],
    *,
    cfg: dict | None = None,
    risk_cfg: dict | None = None,
    fundamentals: dict[str, Any] | None = None,
    sentiment: dict[str, Any] | None = None,
    smart_money: dict[str, Any] | None = None,
) -> dict[str, Score]:
    """
    Score every symbol. Momentum and liquidity are ranked cross-sectionally
    against the rest of the universe scored in this same run, so a score is
    always "relative to today's opportunity set" rather than an absolute the
    market can drift away from.
    """
    cfg = cfg or config.scoring()
    risk_cfg = risk_cfg or config.risk()
    neutral = float(cfg.get("neutral_when_missing", 50))
    weights = cfg["weights"]
    min_bars = max(cfg["trend"]["slow_ma"], max(cfg["momentum"]["lookbacks"])) + 1

    feats = {sym: extract_features(bars, cfg) for sym, bars in bars_by_symbol.items()}

    # Cross-sectional populations, built only from symbols with enough history.
    usable = {s: f for s, f in feats.items() if f.bars_available >= min_bars}
    mom_raw = {s: _blended_momentum(f, cfg) for s, f in usable.items()}
    mom_pop = [v for v in mom_raw.values() if v is not None]
    liq_pop = [f.median_dollar_volume for f in usable.values() if f.median_dollar_volume > 0]

    out: dict[str, Score] = {}
    for sym, f in feats.items():
        unscored: list[str] = []
        comp: dict[str, float] = {}

        if f.bars_available < min_bars:
            out[sym] = Score(
                symbol=sym, asset_class=config.class_of(sym), total=0.0,
                components={}, unscored=list(weights), features=f, eligible=False,
                reason=f"only {f.bars_available} bars, need {min_bars}",
            )
            continue

        raw_mom = mom_raw.get(sym)
        if raw_mom is None:
            comp["momentum"] = neutral
            unscored.append("momentum")
        else:
            comp["momentum"] = round(_percentile_rank(raw_mom, mom_pop), 2)

        comp["trend"] = round(_trend_score(f, cfg), 2)

        if f.annual_vol > 0:
            comp["volatility"] = round(_volatility_score(f.annual_vol, cfg), 2)
        else:
            comp["volatility"] = neutral
            unscored.append("volatility")

        if f.median_dollar_volume > 0 and liq_pop:
            comp["liquidity"] = round(_percentile_rank(f.median_dollar_volume, liq_pop), 2)
        else:
            comp["liquidity"] = neutral
            unscored.append("liquidity")

        # Valuation, quality and catalyst come from fundamentals. An ETF or a
        # crypto pair has no earnings or margins, so those stay UNSCORED
        # rather than being handed a number that looks measured.
        #
        # Smart money is the same shape: a symbol nobody disclosed a trade in
        # is UNSCORED, not neutral. Silence from Congress is absence of data,
        # not a considered hold.
        fund = (fundamentals or {}).get(sym)
        senti = (sentiment or {}).get(sym)
        for name, fn, arg in (("valuation", fu.valuation_score, fund),
                              ("quality", fu.quality_score, fund),
                              ("catalyst", fu.catalyst_score, fund),
                              ("sentiment", sent.sentiment_score, senti),
                              ("smart_money",
                               lambda _s=sym: sm.smart_money_score(_s, smart_money),
                               smart_money)):
            value = (fn() if name == "smart_money" else fn(arg)) if arg is not None else None
            if value is None:
                comp[name] = neutral
                unscored.append(name)
            else:
                comp[name] = value

        # Renormalise over what was actually measured.
        #
        # Filling an unscorable component with a neutral 50 and then weighting
        # it drags every score toward 50. With fundamentals unavailable that is
        # over half the rubric, and nothing would ever clear the threshold, so
        # a data outage would quietly stop the portfolio investing. Instead the
        # measured components carry the full weight between them, and coverage
        # is reported so a thinly measured score is visible as one.
        measured = {k: v for k, v in weights.items() if k not in unscored}
        measured_weight = sum(measured.values())
        coverage = round(measured_weight, 4)

        if measured_weight <= 0:
            total = 0.0
        else:
            total = sum(comp[k] * weights[k] for k in measured) / measured_weight

        threshold = float(risk_cfg.get("score_threshold", 60))
        min_coverage = float(risk_cfg.get("min_rubric_coverage", 0.4))

        if coverage < min_coverage:
            eligible, reason = False, (
                f"only {coverage:.0%} of the rubric could be measured, "
                f"below the {min_coverage:.0%} minimum"
            )
        elif total < threshold:
            eligible, reason = False, f"score {total:.1f} below threshold {threshold}"
        else:
            eligible, reason = True, ""

        out[sym] = Score(
            symbol=sym,
            asset_class=config.class_of(sym),
            total=round(total, 2),
            components=comp,
            unscored=unscored,
            features=f,
            eligible=eligible,
            reason=reason,
            coverage=coverage,
        )
    return out


def _blended_momentum(f: Features, cfg: dict) -> float | None:
    lookbacks = cfg["momentum"]["lookbacks"]
    lb_weights = cfg["momentum"]["weights"]
    parts, total_w = 0.0, 0.0
    for lb, w in zip(lookbacks, lb_weights):
        r = f.returns.get(f"r{lb}")
        if r is not None:
            parts += r * w
            total_w += w
    return parts / total_w if total_w else None


def _trend_score(f: Features, cfg: dict) -> float:
    """
    0-100 from three deterministic conditions: price above the fast average,
    fast above slow, and the slow average rising. Partial credit each.
    """
    if not (f.ma_fast and f.ma_slow and f.last_price):
        return float(cfg.get("neutral_when_missing", 50))
    score = 0.0
    score += 35.0 if f.last_price > f.ma_fast else 0.0
    score += 35.0 if f.ma_fast > f.ma_slow else 0.0
    score += _clamp(50.0 + f.ma_slow_slope * 1000.0, 0, 30)
    return _clamp(score)


def _volatility_score(annual_vol: float, cfg: dict) -> float:
    """Lower realised vol scores higher, on the absolute scale from config."""
    floor = float(cfg["volatility"]["floor"])
    ceiling = float(cfg["volatility"]["ceiling"])
    if annual_vol <= floor:
        return 100.0
    if annual_vol >= ceiling:
        return 0.0
    return _clamp(100.0 * (ceiling - annual_vol) / (ceiling - floor))
