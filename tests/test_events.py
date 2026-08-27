"""
The event path: prefilter, Kelly sizing, and every autopilot guardrail.

These run without an Anthropic key, an Alpaca key or a network, by injecting
the assessment the classifier would have returned. That is the point: the
guardrails have to be verifiable without depending on what a model says on
any given day.

The scenario throughout is the one that prompted this feature, a very large
legal settlement landing on a held position.
"""
from __future__ import annotations

import datetime as dt

import pytest

import run_event
from src import config, events, kelly, risk
from src.brokers.mock import MockBroker
from src.brokers.registry import Registry
from src.data.news import NewsItem
from src.events import Assessment
from src.state import Store


NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "snapshots").mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def broker(tmp_path):
    b = MockBroker("mock", seed=20260826, state_path=tmp_path / "mock.json")
    b.reset()
    return b


class FakeRegistry:
    def __init__(self, broker):
        self._b = broker

    def for_symbol(self, symbol):
        return self._b

    def active(self):
        return {self._b.name: self._b}


def assessment(**kw) -> Assessment:
    base = dict(
        symbol="META", item_id="abc123",
        headline="Meta settles shareholder lawsuit for $19 billion",
        url="https://example.test/meta", published_at=NOW.isoformat(),
        material=True, materiality=0.85, confidence=0.75,
        direction="bearish", thesis_impact="weakens", action="REDUCE",
        probability=0.6, upside=0.10, downside=0.06,
        rationale="A 19bn cash settlement is a real hit to the balance sheet.",
        counter="One time charge, does not change the advertising business.",
        exit_rule="Exit if it closes below the 200 day average.",
        invalidation="Thesis breaks if regulators force a structural remedy.",
        source="claude", prefilter_score=0.95,
    )
    base.update(kw)
    return Assessment(**base)


# --------------------------------------------------------------------------
# prefilter
# --------------------------------------------------------------------------

def test_large_settlement_clears_the_prefilter():
    item = NewsItem("i1", "META",
                    "Meta settles shareholder lawsuit for $19 billion",
                    "", "test", "", NOW)
    assert events.prefilter_score(item) >= 0.9


def test_routine_price_chatter_is_filtered_out():
    for headline in (
        "Apple shares slip in quiet trading",
        "3 reasons to watch tech stocks this week",
        "Analyst raises Microsoft price target to $520",
    ):
        item = NewsItem("x", "AAPL", headline, "", "test", "", NOW)
        assert events.prefilter_score(item) < events.PREFILTER_THRESHOLD, headline


def test_stale_news_is_discounted():
    old = NOW - dt.timedelta(days=5)
    fresh = NewsItem("a", "META", "Meta cuts guidance", "", "t", "", NOW)
    stale = NewsItem("b", "META", "Meta cuts guidance", "", "t", "", old)
    assert events.prefilter_score(stale) < events.prefilter_score(fresh)


def test_seen_items_are_not_reassessed():
    item = NewsItem("dup", "META", "Meta settles lawsuit for $19 billion",
                    "", "t", "", NOW)
    assert events.shortlist([item]) != []
    assert events.shortlist([item], seen={"dup"}) == []


def test_no_llm_key_degrades_to_hold_not_to_keyword_trading(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    item = NewsItem("i", "META", "Meta settles lawsuit for $19 billion",
                    "", "t", "", NOW)
    out = events.classify([(item, 0.95)], {}, {})
    assert len(out) == 1
    assert out[0].action == "HOLD"
    assert out[0].material is False
    assert out[0].probability is None


# --------------------------------------------------------------------------
# Kelly
# --------------------------------------------------------------------------

def test_kelly_refuses_without_an_estimate():
    s = kelly.size(None, None, None)
    assert s.ok is False and "no probability" in s.reason


def test_kelly_refuses_a_negative_edge():
    # 40% chance of +5% against 60% chance of -5% is a losing bet.
    s = kelly.size(0.40, 0.05, 0.05)
    assert s.ok is False and "no edge" in s.reason


def test_kelly_is_fractional_not_full():
    p, up, down = 0.60, 0.10, 0.06
    full = kelly.full_kelly(p, up, down)
    s = kelly.size(p, up, down)
    fraction = float(config.risk()["kelly"]["fraction"])
    assert s.ok
    assert s.fractional_kelly == pytest.approx(full * fraction, rel=1e-6)
    assert s.weight < full, "fractional Kelly must be smaller than full Kelly"


def test_kelly_can_never_exceed_the_position_cap():
    """
    With quarter Kelly this is structural, not incidental: full Kelly is
    bounded by 1.0, so a quarter of it can never exceed 25%, which is exactly
    max_single_position. Even a near certain bet cannot size past the cap.
    """
    cap = float(config.risk()["limits"]["max_single_position"])
    fraction = float(config.risk()["kelly"]["fraction"])
    for p, up, down in [(0.95, 0.50, 0.02), (0.99, 1.00, 0.01), (0.80, 0.30, 0.05)]:
        s = kelly.size(p, up, down)
        assert s.weight <= cap + 1e-9, (p, up, down, s)
        assert s.full_kelly <= 1.0 + 1e-9
    assert fraction * 1.0 <= cap + 1e-9, (
        "raising kelly.fraction above max_single_position would let Kelly "
        "size past the position cap; the code clamps, but the config should "
        "not depend on that"
    )


def test_kelly_clamps_when_the_fraction_is_raised_past_the_cap():
    """The clamp is real code, not just an artefact of the default fraction."""
    cfg = config.risk()
    loud = {**cfg, "kelly": {**cfg["kelly"], "fraction": 0.9}}
    cap = float(cfg["limits"]["max_single_position"])
    s = kelly.size(0.95, 0.50, 0.02, risk_cfg=loud)
    assert s.ok
    assert s.weight == pytest.approx(cap)
    assert s.capped_by == "max_single_position"


def test_kelly_respects_headroom_on_an_existing_position():
    cap = float(config.risk()["limits"]["max_single_position"])
    s = kelly.size(0.95, 0.50, 0.02, current_weight=cap)
    assert s.weight <= cap + 1e-9


def test_kelly_declines_a_position_too_small_to_bother_with():
    s = kelly.size(0.51, 0.02, 0.02)   # a wafer thin edge
    assert s.ok is False and "floor" in s.reason


# --------------------------------------------------------------------------
# autopilot guardrails
# --------------------------------------------------------------------------

def _decide(a, store, broker, *, armed=True, positions=None, equity=10_000.0):
    return run_event._decide(
        a, positions or {}, equity, store, config.risk(),
        FakeRegistry(broker), armed=armed, dry_run=False,
    )


def test_disarmed_autopilot_queues_instead_of_trading(store, broker):
    d = _decide(assessment(action="OPEN"), store, broker, armed=False)
    assert d["executed"] is False
    assert "autopilot disarmed" in d["proposal"]["decision_note"]


def test_hold_does_nothing_at_all(store, broker):
    d = _decide(assessment(action="HOLD"), store, broker)
    assert d["executed"] is False and d["proposal"] is None


def test_low_materiality_is_queued_not_traded(store, broker):
    d = _decide(assessment(action="OPEN", materiality=0.30), store, broker)
    assert d["executed"] is False
    assert "materiality" in d["proposal"]["decision_note"]


def test_low_confidence_is_queued_not_traded(store, broker):
    d = _decide(assessment(action="OPEN", confidence=0.20), store, broker)
    assert d["executed"] is False
    assert "confidence" in d["proposal"]["decision_note"]


def test_missing_kelly_estimate_is_queued_not_traded(store, broker):
    d = _decide(assessment(action="OPEN", probability=None,
                           upside=None, downside=None), store, broker)
    assert d["executed"] is False
    assert "Kelly" in d["proposal"]["decision_note"]


def test_an_armed_material_event_executes(store, broker):
    d = _decide(assessment(action="OPEN", materiality=0.85, confidence=0.75,
                           probability=0.62, upside=0.12, downside=0.06),
                store, broker)
    assert d["executed"] is True, d["proposal"]["decision_note"]
    assert d["proposal"]["status"] == "executed"
    assert d["proposal"]["order"]["status"] == "filled"
    assert broker.get_positions()[0].symbol == "META"


def test_cooldown_stops_the_same_story_firing_twice(store, broker):
    a = assessment(action="OPEN", probability=0.62, upside=0.12, downside=0.06)
    first = _decide(a, store, broker)
    assert first["executed"] is True
    second = _decide(a, store, broker)
    assert second["executed"] is False
    assert "cooldown" in second["proposal"]["decision_note"]


def test_daily_cap_stops_a_bad_news_day_rewriting_the_book(store, broker):
    cap = int(config.risk()["autopilot"]["max_trades_per_day"])
    for i in range(cap):
        store.record_auto_trade(f"SYM{i}")
    d = _decide(assessment(action="OPEN", probability=0.62,
                           upside=0.12, downside=0.06), store, broker)
    assert d["executed"] is False
    assert "daily automatic trade cap" in d["proposal"]["decision_note"]


def test_oversized_automatic_order_is_clamped_not_refused(store, broker):
    """
    Kelly targets are routinely bigger than one automatic order may be. The
    per trade cap must size the order down and leave the rest for later, not
    veto the entry, or autopilot would never open anything.
    """
    cap = float(config.risk()["autopilot"]["max_notional_per_trade_pct"])
    a = assessment(action="OPEN", probability=0.90, upside=0.40, downside=0.03)
    d = _decide(a, store, broker)
    assert d["executed"] is True, d["proposal"]["decision_note"]
    assert abs(d["proposal"]["delta_weight"]) <= cap + 1e-9
    assert d["proposal"]["kelly"]["uncapped_target"] > cap


def test_live_account_refuses_automatic_execution(store, broker):
    broker.mode = "live"
    d = _decide(assessment(action="OPEN", probability=0.62,
                           upside=0.12, downside=0.06), store, broker)
    assert d["executed"] is False
    assert "live account" in d["proposal"]["decision_note"]


def test_exit_is_not_kelly_sized(store, broker):
    d = _decide(assessment(action="EXIT", probability=None, upside=None,
                           downside=None), store, broker,
                positions={}, equity=10_000.0)
    # No Kelly estimate must not block an exit the way it blocks an entry.
    note = (d["proposal"] or {}).get("decision_note", "")
    assert "Kelly" not in note


def test_invalidated_thesis_overrides_the_holding_period(store, broker):
    weakens = assessment(action="EXIT", thesis_impact="weakens")
    breaks = assessment(action="EXIT", thesis_impact="invalidates")
    d1 = _decide(weakens, store, broker)
    d2 = _decide(breaks, store, broker)
    assert d1["proposal"]["override_holding_period"] is False
    assert d2["proposal"]["override_holding_period"] is True


def test_event_proposal_carries_its_source_headline(store, broker):
    d = _decide(assessment(action="OPEN", probability=0.62,
                           upside=0.12, downside=0.06), store, broker)
    ev = d["proposal"]["event"]
    assert "19 billion" in ev["headline"]
    assert ev["url"].startswith("https://")
    assert d["proposal"]["kelly"]["full_kelly"] > 0
