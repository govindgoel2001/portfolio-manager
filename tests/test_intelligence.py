"""
Tests for the parts added on top of the original pipeline: smart money
scoring, the committee tally, manual entry, and retrieval.

These are mostly guardrail tests. The interesting failure mode in all four is
not an exception, it is a plausible looking number produced from nothing, so
most of what is asserted here is that absence stays absent rather than
quietly becoming a neutral 50 or a confident consensus.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src import committee, config, manual, rag, state
from src.data import smartmoney as sm


# --------------------------------------------------------------------------
# smart money
# --------------------------------------------------------------------------

def _trade(**kw):
    base = dict(source="insider", actor="Someone", symbol="AAPL",
                direction="BUY", value_usd=100_000.0,
                traded_on=dt.date.today().isoformat(),
                filed_on=dt.date.today().isoformat())
    base.update(kw)
    return sm.Trade(**base)


def test_no_disclosures_is_unscored_not_neutral():
    """
    Silence is absence of data, not a considered hold. If this ever returned
    50 the rubric would treat "nobody filed anything" as a real measurement.
    """
    signal = sm._score_symbol("AAPL", [], config.load("smartmoney"))
    assert signal.score is None
    assert sm.smart_money_score("AAPL", {"AAPL": signal}) is None


def test_score_is_weighted_by_dollars_not_by_count():
    """One large purchase should outweigh several small routine sales."""
    cfg = config.load("smartmoney")
    trades = [_trade(direction="BUY", value_usd=10_000_000, actor="Big Fund",
                     source="fund")]
    trades += [_trade(direction="SELL", value_usd=50_000, actor=f"Insider {i}")
               for i in range(5)]
    signal = sm._score_symbol("AAPL", trades, cfg)
    assert signal.score > 50, "dollar weight should dominate a count of small sells"
    assert signal.sellers == 5 and signal.buyers == 1
    assert "weighted by dollars" in signal.note


def test_conviction_shrinks_a_thin_signal():
    """A single disclosure must not produce a maximal score."""
    cfg = config.load("smartmoney")
    one = sm._score_symbol("AAPL", [_trade(value_usd=5_000_000)], cfg)
    many = sm._score_symbol("AAPL", [_trade(value_usd=5_000_000)] * 8, cfg)
    assert one.score < many.score
    assert one.score < 100


def test_old_disclosures_decay():
    cfg = config.load("smartmoney")
    old = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    recent = sm._score_symbol("AAPL", [_trade()] * 6, cfg)
    stale = sm._score_symbol("AAPL", [_trade(traded_on=old)] * 6, cfg)
    assert stale.score < recent.score


def test_ptr_row_parses_a_real_filing_line():
    """
    The exact shape produced by pypdf on an electronically filed PTR, dates
    running together because the PDF cells have no separating space.
    """
    text = ("Alphabet Inc. - Class A Common Stock (GOOGL) [ST] "
            "P 07/17/202608/06/2026$1,001 - $15,000")
    m = sm._PTR_ROW.search(text)
    assert m is not None
    assert m.group("ticker") == "GOOGL"
    assert m.group("action") == "P"
    assert sm._to_iso(m.group("traded")) == "2026-07-17"
    assert sm._money(m.group("high")) == 15000.0


# --------------------------------------------------------------------------
# committee
# --------------------------------------------------------------------------

def _seat(name, stance, conviction=7.0, answered=True):
    s = committee.Seat(name=name, model=f"test/{name}")
    s.stance, s.conviction, s.answered = stance, conviction, answered
    return s


def test_unanimous_committee_has_zero_disagreement():
    cfg = config.load("committee")
    panel = [_seat(f"m{i}", "BUY") for i in range(4)]
    v = committee._tally("AAPL", panel, [], cfg)
    assert v.consensus == "BUY"
    assert v.disagreement == 0.0
    assert v.multiplier > 1.0


def test_split_committee_gets_no_influence():
    """
    An evenly split panel is an argument for the default, not for a bigger
    bet. The multiplier must be exactly 1.0.
    """
    cfg = config.load("committee")
    panel = [_seat("a", "BUY"), _seat("b", "BUY"),
             _seat("c", "AVOID"), _seat("d", "AVOID")]
    v = committee._tally("AAPL", panel, [], cfg)
    assert v.disagreement == 100.0
    assert v.multiplier == 1.0


def test_too_few_seats_is_not_a_consensus():
    cfg = config.load("committee")
    panel = [_seat("a", "BUY"), _seat("b", "", answered=False),
             _seat("c", "", answered=False), _seat("d", "", answered=False)]
    v = committee._tally("AAPL", panel, [], cfg)
    assert v.consensus == "NO CONSENSUS"
    assert v.multiplier == 1.0


def test_conviction_averages_only_the_seats_that_agree():
    """
    Averaging a conviction 9 BUY with a conviction 9 AVOID would report 9 for
    a deadlocked committee, which reads as confidence rather than conflict.
    """
    cfg = config.load("committee")
    panel = [_seat("a", "BUY", 9.0), _seat("b", "BUY", 9.0),
             _seat("c", "BUY", 9.0), _seat("d", "AVOID", 1.0)]
    v = committee._tally("AAPL", panel, [], cfg)
    assert v.consensus == "BUY"
    assert v.score == 9.0


def test_multiplier_never_leaves_the_configured_band():
    cfg = config.load("committee")
    lo = cfg["influence"]["min_multiplier"]
    hi = cfg["influence"]["max_multiplier"]
    for stance in ("BUY", "AVOID", "HOLD"):
        for conviction in (0.0, 5.0, 10.0):
            panel = [_seat(f"m{i}", stance, conviction) for i in range(4)]
            v = committee._tally("AAPL", panel, [], cfg)
            assert lo <= v.multiplier <= hi


def test_no_seat_answered_is_reported_as_such():
    cfg = config.load("committee")
    panel = [_seat(f"m{i}", "", answered=False) for i in range(4)]
    v = committee._tally("AAPL", panel, [], cfg)
    assert v.answered == 0
    assert v.consensus == "NO CONSENSUS"
    assert v.multiplier == 1.0
    assert "no seat answered" in v.note


def test_a_seat_never_audits_its_own_claim():
    claims = [committee.Claim(text="x", by="A"), committee.Claim(text="y", by="B")]
    mine = [i for i, c in enumerate(claims) if c.by != "A"]
    assert mine == [1]


# --------------------------------------------------------------------------
# manual entry
# --------------------------------------------------------------------------

class _Acct:
    equity = 100_000.0


@pytest.fixture()
def store(tmp_path):
    return state.Store(tmp_path)


def _req(**kw):
    base = dict(symbol="AAPL", side="BUY", notional=3000.0,
                exit_rule="ROIC below 20% for two quarters",
                invalidation="services revenue declines year on year")
    base.update(kw)
    return manual.Request(**base)


def test_manual_order_cannot_leave_the_universe(store):
    # A symbol that is not, and will not become, a real listing. TSLA used to
    # stand in here and then joined the universe with the S&P 200 screen.
    d = manual.submit(_req(symbol="ZZZZFAKE"), account=_Acct(), positions={}, store=store)
    assert not d.accepted
    assert "not in the universe" in d.reasons[0]


def test_manual_order_requires_an_exit_rule_and_an_invalidation(store):
    d = manual.submit(_req(exit_rule="", invalidation=""),
                      account=_Acct(), positions={}, store=store)
    assert not d.accepted
    assert "exit_rule" in d.reasons[0] and "invalidation" in d.reasons[0]


def test_manual_order_obeys_the_position_limit(store):
    limit = config.risk()["limits"]["max_single_position"]
    d = manual.submit(_req(notional=None, target_weight=limit + 0.2),
                      account=_Acct(), positions={}, store=store)
    assert not d.accepted


def test_manual_order_rejects_two_ways_of_saying_size(store):
    d = manual.submit(_req(target_weight=0.05), account=_Acct(),
                      positions={}, store=store)
    assert not d.accepted
    assert "exactly one" in d.reasons[0]


def test_accepted_manual_order_is_queued_pending_not_executed(store):
    d = manual.submit(_req(), account=_Acct(), positions={}, store=store)
    assert d.accepted
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["status"] == state.PENDING
    assert pending[0]["source"] == "manual"


def test_a_sell_cannot_raise_the_weight(store):
    d = manual.submit(_req(side="SELL", notional=None, target_weight=0.2),
                      account=_Acct(), positions={}, store=store)
    assert not d.accepted
    assert "cannot raise" in d.reasons[0]


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------

def test_chunking_overlaps_so_a_fact_is_not_cut_in_half():
    text = ". ".join(f"Sentence number {i} with some filler words" for i in range(80))
    chunks = rag._chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= rag.CHUNK_CHARS + 40 for c in chunks)


def test_index_reports_whether_it_is_actually_semantic():
    name = rag.backend_name()
    assert "degraded" in name or rag.embedder().semantic


def test_search_on_an_empty_index_returns_nothing():
    index = rag.Index()
    assert index.search("anything") == []


def test_symbol_filter_excludes_other_symbols():
    index = rag.Index()
    index.add("news", "Apple margins expanded on services mix", symbol="AAPL")
    index.add("news", "Nvidia data centre revenue grew sharply", symbol="NVDA")
    hits = index.search("revenue", k=5, symbol="NVDA")
    assert hits
    assert all(h.document.symbol in ("NVDA", "") for h in hits)
