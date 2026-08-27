"""
The validation suite from the build guide.

Four of the five named tests are implemented here. The fifth, the
adversarial-news test, needs a news layer that does not exist yet, and a test
that pretends to exercise a layer that is not wired would be worse than no test
at all. It is written as an explicit skip so it shows up in the run.

    python -m pytest tests/ -v
"""
from __future__ import annotations

import copy
import json

import pytest

from src import allocate, config, risk, score
from src.brokers.base import OrderRequest, verify_adapter
from src.brokers.mock import MockBroker
from src.brokers.registry import Registry, load_adapter


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def broker(tmp_path):
    return MockBroker("mock", seed=20260826, state_path=tmp_path / "mock_state.json")


@pytest.fixture
def bars(broker):
    # Must match collect.BAR_HISTORY_DAYS. The buy and hold rubric looks back
    # 252 trading days and uses a 200 day average, so a 260 calendar day
    # window is not enough history to score anything.
    from src.collect import BAR_HISTORY_DAYS
    return {s: broker.get_bars([s], BAR_HISTORY_DAYS)[s.upper()]
            for s in config.all_symbols()}


@pytest.fixture
def bar_dicts(bars):
    return {
        s: [{"close": b.close, "volume": b.volume, "t": b.ts.isoformat()} for b in series]
        for s, series in bars.items()
    }


class FakeAccount:
    def __init__(self, equity=10_000.0):
        self.equity = equity
        self.cash = equity
        self.buying_power = equity


# --------------------------------------------------------------------------
# 1. replay test
# --------------------------------------------------------------------------

def test_replay_produces_identical_scores(bar_dicts):
    """Same frozen snapshot in, byte-identical scores out. Twice."""
    a = score.score_universe(bar_dicts)
    b = score.score_universe(copy.deepcopy(bar_dicts))
    dump = lambda r: json.dumps({k: v.to_dict() for k, v in sorted(r.items())}, sort_keys=True)
    assert dump(a) == dump(b)


def test_replay_produces_identical_allocation(bar_dicts):
    scores = score.score_universe(bar_dicts)
    first = allocate.build(scores, [], 10_000.0)
    second = allocate.build(score.score_universe(bar_dicts), [], 10_000.0)
    assert [p.to_dict() for p in first] == [p.to_dict() for p in second]


# --------------------------------------------------------------------------
# 2. missing-data test
# --------------------------------------------------------------------------

def test_missing_bars_are_flagged_not_guessed(bar_dicts):
    crippled = dict(bar_dicts)
    crippled["AAPL"] = crippled["AAPL"][:5]      # far too little history
    crippled["NVDA"] = []                         # nothing at all

    scores = score.score_universe(crippled)
    assert scores["AAPL"].eligible is False
    assert "bars" in scores["AAPL"].reason
    assert scores["NVDA"].eligible is False
    assert scores["NVDA"].total == 0.0


def test_unscorable_components_are_declared(bar_dicts):
    """
    With no fundamentals passed in, the fundamentals-derived components must
    declare themselves unscored rather than passing off a neutral as a reading.
    """
    scores = score.score_universe(bar_dicts)
    msft = scores["MSFT"]
    for component in ("valuation", "quality", "catalyst"):
        assert component in msft.unscored
    neutral = config.scoring()["neutral_when_missing"]
    assert msft.components["valuation"] == neutral


def test_score_is_renormalised_over_measured_components(bar_dicts):
    """
    A neutral filler must not drag the score toward 50. If it did, a
    fundamentals outage would silently stop the portfolio investing, because
    over half the rubric would sit at neutral and nothing would clear the
    threshold.
    """
    scores = score.score_universe(bar_dicts)
    weights = config.scoring()["weights"]
    msft = scores["MSFT"]

    measured = {k: v for k, v in weights.items() if k not in msft.unscored}
    expected = (sum(msft.components[k] * weights[k] for k in measured)
                / sum(measured.values()))
    assert msft.total == pytest.approx(expected, abs=0.01)
    assert msft.coverage == pytest.approx(sum(measured.values()), abs=1e-6)

    # And the system still finds something to own on price data alone.
    assert any(s.eligible for s in scores.values()), (
        "no symbol was eligible without fundamentals, so a yfinance outage "
        "would stop the portfolio investing entirely"
    )


def test_too_little_rubric_coverage_makes_a_symbol_ineligible(bar_dicts):
    strict = {**config.risk(), "min_rubric_coverage": 0.95}
    scores = score.score_universe(bar_dicts, risk_cfg=strict)
    assert all(not s.eligible for s in scores.values())
    assert any("rubric could be measured" in s.reason
               for s in scores.values() if s.reason)


def test_etfs_and_crypto_get_no_fabricated_fundamentals():
    from src.data.fundamentals import Fundamentals, valuation_score, quality_score

    etf = Fundamentals("GLD", quote_type="ETF", price_to_book=1.02)
    crypto = Fundamentals("BTC/USD", error="no fundamentals for a crypto pair")
    equity = Fundamentals("MSFT", quote_type="EQUITY", forward_pe=21.0,
                          profit_margin=0.40)

    assert valuation_score(etf) is None, "an ETF has no meaningful price to book"
    assert valuation_score(crypto) is None
    assert quality_score(etf) is None
    assert valuation_score(equity) is not None


def test_stale_prices_block_a_proposal():
    class StaleQuote:
        price = 100.0
        age_hours = 300.0

    proposal = {
        "symbol": "GLD", "asset_class": "gold", "action": "OPEN",
        "current_weight": 0.0, "target_weight": 0.10,
        "exit_rule": "x", "invalidation": "y",
    }
    verdict = risk.evaluate([proposal], account=FakeAccount(),
                            quotes={"GLD": StaleQuote()})
    assert "GLD" in verdict.blocked
    assert "stale" in verdict.blocked["GLD"]


# --------------------------------------------------------------------------
# 3. risk-limit test
# --------------------------------------------------------------------------

def test_oversized_position_is_rejected_by_code():
    cap = config.risk()["limits"]["max_single_position"]
    proposal = {
        "symbol": "NVDA", "asset_class": "us_stocks", "action": "OPEN",
        "current_weight": 0.0, "target_weight": cap + 0.10,
        "exit_rule": "x", "invalidation": "y",
    }
    verdict = risk.evaluate([proposal], account=FakeAccount())
    # Symbol scope: the name is blocked, the run itself is not void.
    assert "NVDA" in verdict.blocked
    assert any(c.rule == "max_single_position" and not c.passed
               for c in verdict.checks)
    ok, why = risk.approvable(proposal, verdict)
    assert ok is False and "max_single_position" in why


def test_cash_floor_is_enforced():
    limits = config.risk()["limits"]
    # Four names each just under the single-name cap, which blows the cash floor.
    proposals = [
        {"symbol": s, "asset_class": "us_stocks", "action": "OPEN",
         "current_weight": 0.0, "target_weight": 0.24,
         "exit_rule": "x", "invalidation": "y"}
        for s in ("AAPL", "MSFT", "NVDA", "AMZN")
    ]
    verdict = risk.evaluate(proposals, account=FakeAccount())
    failed = {c.rule for c in verdict.failures}
    assert "min_cash" in failed or "max_class_exposure" in failed
    assert verdict.passed is False


def test_missing_exit_rule_blocks_the_proposal():
    proposal = {
        "symbol": "GLD", "asset_class": "gold", "action": "OPEN",
        "current_weight": 0.0, "target_weight": 0.10,
        "exit_rule": "", "invalidation": "y",
    }
    verdict = risk.evaluate([proposal], account=FakeAccount())
    assert "GLD" in verdict.blocked
    assert "exit rule" in verdict.blocked["GLD"]


def test_allocator_never_exceeds_its_own_limits(bar_dicts):
    limits = config.risk()["limits"]
    scores = score.score_universe(bar_dicts)
    proposals = allocate.build(scores, [], 10_000.0)
    total = sum(p.target_weight for p in proposals)
    assert total <= limits["max_gross_exposure"] + 1e-9
    assert 1.0 - total >= limits["min_cash"] - 1e-9
    for p in proposals:
        assert p.target_weight <= limits["max_single_position"] + 1e-9


def test_auto_execution_is_refused_on_a_live_account():
    proposal = {
        "symbol": "AAPL", "asset_class": "us_stocks", "action": "OPEN",
        "current_weight": 0.0, "target_weight": 0.10,
        "exit_rule": "x", "invalidation": "y",
    }
    verdict = risk.evaluate([proposal], account=FakeAccount(), broker_mode="live")
    assert verdict.auto_execute_allowed is False


# --------------------------------------------------------------------------
# 4. no-trade test
# --------------------------------------------------------------------------

class Pos:
    def __init__(self, symbol, market_value):
        self.symbol = symbol
        self.market_value = market_value


def test_allocator_converges_and_then_stops_trading(bar_dicts):
    """
    On unchanged data the system must settle and go quiet. Per-run caps on new
    positions and turnover mean it takes several runs to become fully invested,
    so the property under test is convergence, not stillness on run two. A
    system that never stops proposing changes on identical input is churning.
    """
    scores = score.score_universe(bar_dicts)
    equity = 10_000.0
    held: list[Pos] = []

    for run in range(1, 16):
        proposals = allocate.build(scores, held, equity)
        changes = [p for p in proposals if p.action != "KEEP"]
        if not changes:
            assert run > 1, "allocator did nothing on an empty portfolio"
            return
        # Simulate every proposal filling exactly at its target.
        held = [Pos(p.symbol, p.target_weight * equity)
                for p in proposals if p.target_weight > 0]

    pytest.fail(f"allocator never converged: still proposing "
                f"{[p.symbol for p in changes]} after 15 runs on identical data")


def test_settled_portfolio_proposes_no_change(bar_dicts):
    """Once converged, feeding the same scores back must return NO ACTION."""
    scores = score.score_universe(bar_dicts)
    equity = 10_000.0
    held: list[Pos] = []
    for _ in range(15):
        proposals = allocate.build(scores, held, equity)
        if not [p for p in proposals if p.action != "KEEP"]:
            break
        held = [Pos(p.symbol, p.target_weight * equity)
                for p in proposals if p.target_weight > 0]

    again = allocate.build(scores, held, equity)
    changes = [p for p in again if p.action != "KEEP"]
    assert changes == [], f"expected no changes, got {[p.symbol for p in changes]}"


def test_empty_opportunity_set_proposes_nothing():
    proposals = allocate.build({}, [], 10_000.0)
    assert proposals == []


# --------------------------------------------------------------------------
# 5. adversarial news test
# --------------------------------------------------------------------------

@pytest.mark.skip(reason="no news layer is wired; see prompts/research.md")
def test_sensational_headlines_do_not_move_the_score():
    ...


# --------------------------------------------------------------------------
# broker seam
# --------------------------------------------------------------------------

def test_every_configured_adapter_satisfies_the_protocol():
    for key, spec in config.brokers()["brokers"].items():
        cls = load_adapter(spec["adapter"])
        assert verify_adapter(cls) == [], f"{key} is not a valid adapter"


def test_registry_falls_back_when_a_broker_has_no_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    config.reset_cache()
    reg = Registry()
    assert reg.for_class("us_stocks").name == "mock"
    assert "alpaca_paper" in reg.failures
    assert reg.degraded() is True


def test_routing_covers_every_universe_class():
    reg = Registry()
    for key in config.universe()["classes"]:
        broker = reg.for_class(key)
        assert broker.can_trade(key), f"{broker.name} cannot trade {key}"


def test_crypto_symbols_round_trip_through_normalisation():
    from src.brokers.alpaca import AlpacaBroker
    b = AlpacaBroker.__new__(AlpacaBroker)  # no network, no credentials
    assert b.denormalize("BTCUSD") == "BTC/USD"
    assert b.denormalize("GLD") == "GLD"
    assert b.normalize("btc/usd") == "BTC/USD"


def test_order_request_rejects_ambiguous_sizing():
    with pytest.raises(ValueError):
        OrderRequest(symbol="AAPL", side="buy")
    with pytest.raises(ValueError):
        OrderRequest(symbol="AAPL", side="buy", notional=100, qty=1)


def test_mock_broker_rejects_a_buy_it_cannot_fund(broker):
    with pytest.raises(Exception) as e:
        broker.submit_order(OrderRequest(symbol="AAPL", side="buy", notional=999_999))
    assert "insufficient cash" in str(e.value)


def test_mock_broker_fill_updates_cash_and_position(broker):
    start = broker.get_account().cash
    broker.submit_order(OrderRequest(symbol="AAPL", side="buy", notional=1000))
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert broker.get_account().cash == pytest.approx(start - 1000, abs=0.01)


# --------------------------------------------------------------------------
# config integrity
# --------------------------------------------------------------------------

def test_scoring_weights_sum_to_one():
    assert sum(config.scoring()["weights"].values()) == pytest.approx(1.0)


def test_risk_limits_are_internally_consistent():
    limits = config.risk()["limits"]
    assert limits["min_cash"] + limits["max_gross_exposure"] <= 1.0 + 1e-9
    assert limits["min_position"] < limits["max_single_position"]


def test_every_universe_symbol_maps_back_to_its_class():
    for key, spec in config.universe()["classes"].items():
        for sym in spec["symbols"]:
            assert config.class_of(sym) == key


# --------------------------------------------------------------------------
# turnover and exits
# --------------------------------------------------------------------------

def test_exits_are_prioritised_not_all_blocked_by_the_turnover_cap():
    """
    Proposing more exits than the turnover cap allows used to fail the whole
    run at the risk gate, so every exit was blocked and the portfolio kept
    holding all of them. The weakest must go first and the rest wait.
    """
    from src.allocate import _fit_turnover

    held = {"A": 0.09, "B": 0.08, "C": 0.07}
    targets = {"A": 0.0, "B": 0.0, "C": 0.0}

    class S:
        def __init__(self, total):
            self.total = total

    scores = {"A": S(70.0), "B": S(40.0), "C": S(55.0)}
    out = _fit_turnover(targets, held, 0.10, scores)

    closed = [s for s, w in out.items() if w == 0.0]
    turnover = sum(abs(out[s] - held[s]) for s in held)
    assert turnover <= 0.10 + 1e-9, f"turnover {turnover} exceeded the cap"
    assert closed, "no exit was taken at all"
    assert "B" in closed, "the weakest name should be closed first"
    assert len(closed) < 3, "all three exits fired despite the cap"
    for sym in held:
        if sym not in closed:
            assert out[sym] == held[sym], "a deferred exit must keep its weight"


def test_a_single_exit_larger_than_the_budget_still_goes_through():
    """A position must never become impossible to close."""
    from src.allocate import _fit_turnover

    held = {"BIG": 0.24}
    out = _fit_turnover({"BIG": 0.0}, held, 0.10, None)
    assert out["BIG"] == 0.0


def test_one_blocked_symbol_does_not_void_the_whole_run():
    """
    A symbol-scope failure blocks that name only. Conflating it with a
    portfolio failure meant a single position inside its holding window
    blocked every unrelated proposal in the same run.
    """
    stuck = {
        "symbol": "COST", "asset_class": "us_stocks", "action": "EXIT",
        "current_weight": 0.06, "target_weight": 0.0,
        "exit_rule": "x", "invalidation": "y",
    }
    fine = {
        "symbol": "SPY", "asset_class": "us_stocks", "action": "OPEN",
        "current_weight": 0.0, "target_weight": 0.04,
        "exit_rule": "x", "invalidation": "y",
    }
    verdict = risk.evaluate([stuck, fine], account=FakeAccount(),
                            held_days={"COST": 0, "SPY": None})

    assert "COST" in verdict.blocked
    assert "SPY" not in verdict.blocked
    assert verdict.passed is True, "a symbol failure must not void the run"

    ok_cost, _ = risk.approvable(stuck, verdict)
    ok_spy, why = risk.approvable(fine, verdict)
    assert ok_cost is False
    assert ok_spy is True, f"SPY was blocked by another symbol's failure: {why}"


def test_a_portfolio_failure_still_voids_the_run():
    proposals = [
        {"symbol": s, "asset_class": "us_stocks", "action": "OPEN",
         "current_weight": 0.0, "target_weight": 0.24,
         "exit_rule": "x", "invalidation": "y"}
        for s in ("AAPL", "MSFT", "NVDA", "AMZN")
    ]
    verdict = risk.evaluate(proposals, account=FakeAccount())
    assert verdict.passed is False


# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------

def test_dockerfile_ships_every_entry_point_that_is_shelled_out_to():
    """
    Processes shell out to scripts by name. If one is missing from the image
    the job fails on every tick while reporting a clean exit code for a file
    that does not exist, which is invisible in the logs. run_event.py was
    missing for 17 hours exactly this way.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    copied = " ".join(
        line for line in dockerfile.splitlines() if line.startswith("COPY")
    )

    callers = [p for p in (root / "src" / "stream.py", root / "scheduler.py")
               if p.exists()]
    assert callers, "no caller found; update this test"

    invoked: set[str] = set()
    for caller in callers:
        invoked |= set(re.findall(r'"(\w+\.py)"', caller.read_text(encoding="utf-8")))
    assert invoked, "found no scripts shelled out to; update this test"

    for script in sorted(invoked):
        assert (root / script).exists(), f"{script} does not exist in the repo"
        assert script in copied, (
            f"a process runs {script} but the Dockerfile never copies it, "
            f"so it will not exist in the image"
        )


def test_llm_defaults_to_hermes_not_a_second_api_key():
    """
    Every reasoning call in the system runs on the opencode subscription
    Hermes already has. Defaulting to the Anthropic API would silently
    reintroduce a second key nobody set.
    """
    from src import llm
    assert llm.BACKEND == "hermes"
    assert llm.HERMES_URL.startswith("http")


def test_a_wide_screening_pool_still_produces_a_book():
    """
    Weights are score proportional, so every name gets smaller as the pool
    grows. Past roughly thirty candidates each weight falls under
    min_position, the dust filter drops all of them, and the allocator
    silently proposes nothing at all.

    The pool is built here rather than taken from the universe so the
    guarantee survives the universe changing size. It was a real failure: a
    two hundred name screen made the allocator go completely quiet with no
    error anywhere.
    """
    limits = config.risk()["limits"]
    pool = 120
    scores = {
        f"SYN{i:03d}": score.Score(
            symbol=f"SYN{i:03d}", asset_class="us_stocks",
            total=95.0 - i * 0.2, components={}, unscored=[],
            features=score.Features(), eligible=True, coverage=1.0,
        )
        for i in range(pool)
    }
    assert len(scores) > 30

    proposals = allocate.build(scores, [], 10_000.0)
    changes = [p for p in proposals if p.action != "KEEP"]
    assert changes, "a wide candidate pool produced no proposals at all"

    for p in changes:
        assert p.target_weight >= limits["min_position"] - 1e-9
        assert p.target_weight <= limits["max_single_position"] + 1e-9
