"""
Tests for the sector map, the tracker leaderboard and the objective.

The failure mode these guard against is the same one throughout: a number that
looks measured but is not. A sector tile reading zero flow because nobody
filed, a leaderboard that quietly drops the losers, an edge figure annualised
from three weeks of data. Each of those produces a confident, wrong dashboard,
and none of them raises an exception.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from src import basket, config, connections, objective, reconcile, trackers
from src.data import smartmoney as sm
from src.data import sectors


# --------------------------------------------------------------------------
# sectors
# --------------------------------------------------------------------------

def test_every_sector_has_a_tradeable_fund():
    """
    A tile the portfolio cannot buy is decoration. Each of the eleven maps to
    an ETF, and every one of those is in the universe.
    """
    universe = {s.upper() for s in config.all_symbols()}
    for etf in sectors.SECTORS:
        assert etf in universe, f"{etf} is on the map but not in the universe"


def test_sector_names_are_unique():
    names = [s["name"] for s in sectors.SECTORS.values()]
    assert len(names) == len(set(names))


def test_sic_fallback_only_targets_real_sectors():
    for sic_name, etf in sectors.SIC_TO_SECTOR.items():
        assert etf in sectors.SECTORS, f"{sic_name} maps to unknown tile {etf}"


def test_a_sector_fund_classifies_as_itself():
    assert sectors.classify("XLK") == "XLK"
    assert sectors.classify("XLE") == "XLE"


def test_yahoo_sector_drives_classification():
    funds = {"AAPL": {"sector": "Technology"},
             "XOM": {"sector": "Energy"},
             "JPM": {"sector": "Financial Services"}}
    assert sectors.classify("AAPL", funds) == "XLK"
    assert sectors.classify("XOM", funds) == "XLE"
    assert sectors.classify("JPM", funds) == "XLF"


def test_an_unknown_symbol_is_unclassified_not_guessed():
    """
    Returning None matters. Dropping an unmapped holding into the nearest tile
    would put dollars in a sector nobody traded.
    """
    assert sectors.classify("ZZZZFAKE", {}) is None


def test_flow_is_attributed_to_the_right_tile():
    tiles = {k: sectors.SectorTile(etf=k, name=v["name"])
             for k, v in sectors.SECTORS.items()}
    signals = {
        "AAPL": {"trades": [
            {"source": "fund", "direction": "BUY", "value_usd": 1_000_000,
             "actor": "A Fund"}]},
        "XOM": {"trades": [
            {"source": "congress", "direction": "SELL", "value_usd": 50_000,
             "actor": "A Member"}]},
    }
    funds = {"AAPL": {"sector": "Technology"}, "XOM": {"sector": "Energy"}}
    unclassified = sectors._flow_layer(tiles, signals, funds)

    assert unclassified == 0
    assert tiles["XLK"].fund_net == 1_000_000
    assert tiles["XLE"].congress_net == -50_000
    assert tiles["XLV"].net_flow == 0
    assert tiles["XLK"].flow_names == 1


def test_unclassifiable_flow_is_counted_not_dropped_silently():
    tiles = {k: sectors.SectorTile(etf=k, name=v["name"])
             for k, v in sectors.SECTORS.items()}
    signals = {"ZZZZFAKE": {"trades": [
        {"source": "fund", "direction": "BUY", "value_usd": 1, "actor": "x"}]}}
    assert sectors._flow_layer(tiles, signals, {}) == 1


# --------------------------------------------------------------------------
# trackers
# --------------------------------------------------------------------------

def _window(ret, bench):
    return trackers.Window(label="w", start="2026-01-01", end="2026-04-01",
                           ret=ret, bench=bench, names=10)


def test_excess_is_the_difference_against_the_benchmark():
    assert _window(10.0, 4.0).excess == 6.0
    assert _window(-2.0, 4.0).excess == -6.0


def test_a_losing_tracker_keeps_its_place_on_the_board():
    """
    The whole point of the board is that it shows the losers. Summarising must
    not silently exclude them.
    """
    t = trackers.Tracker(key="k", name="Loser", kind="fund")
    t.windows = [_window(1.0, 5.0), _window(2.0, 6.0)]
    trackers._summarise(t)
    assert t.excess == -8.28
    assert t.beat_rate == 0.0
    assert not t.error


def test_beat_rate_counts_windows_not_magnitude():
    t = trackers.Tracker(key="k", name="Streaky", kind="fund")
    t.windows = [_window(30.0, 1.0), _window(0.0, 1.0),
                 _window(0.0, 1.0), _window(0.0, 1.0)]
    trackers._summarise(t)
    assert t.beat_rate == 0.25
    assert t.excess > 0          # one huge win still carries the compound


def test_consensus_ignores_managers_who_lost():
    payload = {"trackers": [
        {"name": "Winner", "excess": 3.0, "stale_days": 20,
         "holdings": [{"symbol": "AAA", "weight": 0.5}]},
        {"name": "Loser", "excess": -3.0, "stale_days": 20,
         "holdings": [{"symbol": "BBB", "weight": 0.5}]},
    ]}
    rows = trackers.top_holdings(payload)
    assert [r["symbol"] for r in rows] == ["AAA"]


def test_consensus_ignores_a_stale_book():
    """A manager who stopped filing has a record but no current view."""
    payload = {"trackers": [
        {"name": "Retired", "excess": 9.0,
         "stale_days": trackers.MAX_BOOK_AGE_DAYS + 1,
         "holdings": [{"symbol": "OLD", "weight": 1.0}]},
        {"name": "Active", "excess": 1.0, "stale_days": 20,
         "holdings": [{"symbol": "NEW", "weight": 1.0}]},
    ]}
    assert [r["symbol"] for r in trackers.top_holdings(payload)] == ["NEW"]


def test_an_amendment_is_not_a_quarterly_book():
    """
    A 13F-HR/A restates a few positions, usually the ones that were held back
    under confidential treatment. It is not the manager's portfolio. Berkshire's
    August 2025 amendment disclosed three homebuilders and nothing else, and
    reading it as a quarter turned Berkshire into a housing fund for 92 days.
    """
    recent = {
        "form": ["13F-HR", "13F-HR/A", "13F-HR", "13F-NT"],
        "filingDate": ["2026-08-14", "2025-08-14", "2025-08-14", "2025-05-15"],
        "accessionNumber": ["0-1", "0-2", "0-3", "0-4"],
    }
    rows = sm._rows_for_form(recent, "13F-HR")
    assert [r["form"] for r in rows] == ["13F-HR", "13F-HR"]
    assert [r["accession"] for r in rows] == ["01", "03"]


def test_two_filings_on_one_day_collapse_to_one():
    """
    A multi-manager filer can land two 13F-HRs on the same date. Keeping both
    creates a zero length window, and the loop that skips it silently drops the
    real book that was supposed to start there.
    """
    recent = {
        "form": ["13F-HR", "13F-HR", "13F-HR"],
        "filingDate": ["2026-08-14", "2026-05-15", "2026-05-15"],
        "accessionNumber": ["0-1", "0-2", "0-3"],
    }
    rows = sm._rows_for_form(recent, "13F-HR")
    assert [r["filed"] for r in rows] == ["2026-08-14", "2026-05-15"]


def _win(ret, bench, start, end, names=10, coverage=1.0):
    return trackers.Window(label="w", start=start, end=end, ret=ret,
                           bench=bench, names=names, coverage=coverage)


def test_a_two_week_stub_does_not_count_as_much_as_a_quarter():
    """
    Every fund's last window runs from its newest filing to today, which is a
    fortnight, not a quarter. Averaging that fortnight's excess with four
    quarters gave it a fifth of the weight and let the last two weeks of tape
    decide the whole leaderboard. Compounding gives it the fortnight it earned.
    """
    t = trackers.Tracker(key="k", name="Steady", kind="fund")
    t.windows = [
        _win(10.0, 0.0, "2025-08-14", "2025-11-14"),
        _win(0.0, 0.0, "2025-11-14", "2026-02-14"),
        _win(0.0, 0.0, "2026-02-14", "2026-05-15"),
        _win(0.0, 0.0, "2026-05-15", "2026-08-14"),
        _win(-2.0, 0.0, "2026-08-14", "2026-08-28"),
    ]
    trackers._summarise(t)
    # Arithmetic mean would be (10 - 2) / 5 = 1.60. Compounded is 1.10*0.98 - 1.
    assert t.excess == 7.80
    assert t.span_days == 379
    assert not hasattr(t, "mean_excess")   # the old averaged field is gone


def test_compounding_beats_the_benchmark_over_the_same_dates():
    """The excess is one number over one span, not an average of ratios."""
    t = trackers.Tracker(key="k", name="Pair", kind="fund")
    t.windows = [_win(10.0, 5.0, "2026-01-01", "2026-04-01"),
                 _win(10.0, 5.0, "2026-04-01", "2026-07-01")]
    trackers._summarise(t)
    # book 1.1^2 = 21.00%, bench 1.05^2 = 10.25%
    assert t.excess == 10.75


def test_a_window_that_priced_a_fraction_of_the_book_is_not_a_measurement():
    """
    Renormalising over whatever happened to price turns three homebuilders into
    "Berkshire" and one congressional purchase into "Congress". A window that
    covers less than the floor is dropped and counted, not quietly rescaled.
    """
    t = trackers.Tracker(key="k", name="Thin", kind="fund")
    t.windows = [_win(10.0, 0.0, "2026-01-01", "2026-04-01", names=25, coverage=1.0),
                 _win(0.0, 0.0, "2026-04-01", "2026-07-01", names=25, coverage=1.0),
                 _win(90.0, 0.0, "2026-07-01", "2026-10-01", names=2, coverage=0.08)]
    trackers._summarise(t)
    assert t.excess == 10.0
    assert t.dropped_windows == 1
    assert len(t.windows) == 2


def test_one_surviving_window_is_an_anecdote_not_a_record():
    """
    Dropping the thin windows can leave a single quarter standing. Ranking a
    manager fourth on the board off one 91 day window repeats the mistake the
    coverage floor was added to stop.
    """
    t = trackers.Tracker(key="k", name="OneShot", kind="inverse")
    t.windows = [_win(20.0, 5.0, "2026-01-01", "2026-04-01"),
                 _win(90.0, 0.0, "2026-04-01", "2026-07-01", names=2, coverage=0.1)]
    trackers._summarise(t)
    assert t.excess is None
    assert "record" in t.error


def test_a_tracker_with_no_covered_window_is_unmeasured_not_zero():
    t = trackers.Tracker(key="k", name="AllThin", kind="fund")
    t.windows = [_win(90.0, 0.0, "2026-01-01", "2026-04-01", names=1, coverage=0.04)]
    trackers._summarise(t)
    assert t.excess is None
    assert t.error


# --------------------------------------------------------------------------
# objective
# --------------------------------------------------------------------------

def _curve(start, end, n):
    step = (end / start) ** (1 / (n - 1))
    return [{"t": f"d{i}", "equity": start * (step ** i)} for i in range(n)]


def test_short_samples_are_not_annualised():
    """
    Scaling a few weeks to a yearly rate is noise with a large multiplier on
    it, and the sign of that noise decides whether the product looks good.
    """
    edge = objective.evaluate(_curve(100, 106, 20), _curve(100, 101, 20))
    assert edge is not None
    assert edge.annualised is False
    assert edge.excess_annual_pct is None
    assert edge.verdict == "provisional"


def test_a_long_sample_is_annualised_and_judged():
    edge = objective.evaluate(_curve(100, 130, 252), _curve(100, 110, 252))
    assert edge.annualised is True
    assert edge.excess_annual_pct == pytest.approx(20.0, abs=0.5)
    assert edge.verdict == "ahead of stretch"
    assert edge.beating_benchmark


def test_losing_to_the_benchmark_is_reported_as_losing():
    edge = objective.evaluate(_curve(100, 104, 252), _curve(100, 120, 252))
    assert edge.beating_benchmark is False
    assert edge.verdict == "alarm"
    assert "would have done better" in edge.headline


def test_beating_the_index_but_missing_the_target_is_its_own_verdict():
    """Missing an ambitious target while still beating the index is a good
    outcome, and conflating it with losing would describe it as a bad one."""
    edge = objective.evaluate(_curve(100, 115, 252), _curve(100, 112, 252))
    assert edge.beating_benchmark
    assert edge.verdict == "ahead of benchmark, short of target"


def test_no_curve_measures_nothing():
    assert objective.evaluate([]) is None
    assert objective.evaluate([{"t": "d0", "equity": 100}]) is None


# --------------------------------------------------------------------------
# the basket
# --------------------------------------------------------------------------

class _Score:
    def __init__(self, total, eligible=True, components=None, unscored=None):
        self.total = total
        self.eligible = eligible
        self.components = components or {}
        self.unscored = unscored or []


def test_sleeve_targets_fit_inside_the_deployable_budget():
    """
    Checked at load rather than discovered at build time. Overshooting makes
    the builder scale every holding down proportionally, which produces a
    basket that looks deliberate but is quietly off every target.
    """
    cfg = config.basket()
    total = sum(float(s["target"]) for s in cfg["sleeves"].values())
    budget = min(1.0 - float(cfg["cash"]["min"]),
                 float(config.risk()["limits"]["max_gross_exposure"]))
    assert total <= budget + 1e-9


def test_every_basket_sleeve_names_a_real_universe_class():
    known = set(config.universe()["classes"])
    for key, spec in config.basket()["sleeves"].items():
        for cls_name in spec["classes"]:
            assert cls_name in known, f"sleeve {key} references {cls_name}"


def test_the_benchmark_is_described_as_the_benchmark():
    """
    Holding the index you are measured against guarantees no edge on that
    slice. It is a fine choice and it must not read as a stock pick.
    """
    why = basket._why("SPY", _Score(80), "core_index", [])
    assert "never beating it" in why or "never beat it" in why


def test_an_empty_sleeve_says_so_rather_than_forcing_a_holding():
    scores = {"MSFT": _Score(90)}
    b = basket.build(scores, tracker_board=None)
    empty = [s for s in b.sleeves if not s.picks]
    assert empty, "expected sleeves with no qualifying names"
    assert all(s.note for s in empty)
    assert any("less diversified" in w for w in b.warnings)


def test_cash_floor_is_respected():
    scores = {s: _Score(95) for s in config.all_symbols() if "/" not in s}
    b = basket.build(scores, tracker_board=None)
    assert b.cash >= config.basket()["cash"]["min"] - 1e-6
    assert b.invested + b.cash == pytest.approx(1.0, abs=1e-3)


def test_following_someone_still_filters_through_our_screen():
    """
    A name they hold that this system would not touch does not get in on
    their reputation.
    """
    board = {"trackers": [{
        "name": "Someone", "key": "fund-1", "kind": "fund",
        "excess": 5.0, "beat_rate": 1.0, "stale_days": 10,
        "holdings": [{"symbol": "MSFT", "weight": 0.5},
                     {"symbol": "AAPL", "weight": 0.5}],
    }]}
    scores = {"MSFT": _Score(90), "AAPL": _Score(10, eligible=False)}
    b = basket.build(scores, tracker_board=board, follow="Someone")
    held = {p.symbol for p in b.picks}
    assert "MSFT" in held
    assert "AAPL" not in held, "an ineligible name got in on the manager's name"


def test_following_discloses_how_little_of_their_book_is_reachable():
    board = {"trackers": [{
        "name": "Someone", "key": "fund-1", "kind": "fund",
        "excess": 5.0, "beat_rate": 1.0, "stale_days": 10,
        "holdings": [{"symbol": "MSFT", "weight": 0.5},
                     {"symbol": "NOTINUNIVERSE", "weight": 0.5}],
    }]}
    b = basket.build({"MSFT": _Score(90)}, tracker_board=board, follow="Someone")
    assert any("not their portfolio" in w for w in b.warnings)


def test_only_managers_who_beat_the_index_tilt_the_basket():
    board = {"trackers": [
        {"name": "Winner", "excess": 3.0, "stale_days": 10,
         "holdings": [{"symbol": "AAA", "weight": 1.0}]},
        {"name": "Loser", "excess": -3.0, "stale_days": 10,
         "holdings": [{"symbol": "BBB", "weight": 1.0}]},
    ]}
    backers = basket._backers(board)
    assert backers.get("AAA") == ["Winner"]
    assert "BBB" not in backers


def test_expectations_never_promise_the_target():
    text = " ".join(basket._expectations(0.8, 0.3, 0.2)).lower()
    assert "skeptical" in text
    assert "guarantee" not in text


# --------------------------------------------------------------------------
# connections and import
# --------------------------------------------------------------------------

def test_the_catalog_never_claims_more_than_a_broker_can_do():
    """
    The interface renders these flags directly, so a wrong one is a promise
    the software cannot keep. Groww has no paper account and Hyperliquid is
    read only here on purpose.
    """
    cat = {c["key"]: c for c in connections.catalog()}
    assert cat["groww"]["has_paper"] is False
    assert "paper" not in cat["groww"]["modes"]
    assert cat["hyperliquid"]["can_trade"] is False
    # Read only, deliberately: a connection never routes orders. Execution
    # is a config change and a redeploy, not a pasted form field.
    assert cat["alpaca"]["can_trade"] is False
    assert all(c["can_trade"] is False for c in cat.values())
    assert "paper" in cat["alpaca"]["modes"] and "live" in cat["alpaca"]["modes"]


def test_a_mode_a_broker_does_not_support_is_refused():
    with pytest.raises(ValueError):
        connections.add("groww", "x", "paper",
                        {"api_key": "a", "api_secret": "b"})


def test_credentials_never_reach_the_api():
    conn = connections.Connection(
        id="t", broker="alpaca", label="t",
        credentials={"key_id": "PKABCDEFGH", "secret_key": "supersecret"})
    public = conn.public()
    blob = json.dumps(public)
    assert "supersecret" not in blob
    assert "PKABCDEFGH" not in blob
    assert public["hints"]["secret_key"].endswith("cret")


def test_paste_reads_the_formats_people_actually_use():
    rows, bad = reconcile.parse("AAPL 10\nMSFT,5,2100\nGLD\t8\t2400")
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT", "GLD"]
    assert rows[1]["market_value"] == 2100
    assert not bad


def test_an_unreadable_line_is_returned_not_guessed():
    """A wrong quantity is worse than a missing one."""
    rows, bad = reconcile.parse("AAPL 10\nthis is not a holding")
    assert len(rows) == 1
    assert bad == ["this is not a holding"]


class _Pick:
    def __init__(self, symbol, weight, why=""):
        self.symbol, self.weight, self.why = symbol, weight, why


class _Basket:
    def __init__(self, picks):
        self.picks = picks


def test_a_holding_outside_the_universe_is_not_a_sell():
    """
    Silence about an unknown asset is missing information. Reporting it as a
    sell would be the system mistaking the edge of its own coverage for a view.
    """
    report = reconcile.build(
        [{"symbol": "ZZZZFAKE", "qty": 1, "market_value": 100}],
        _Basket([]), {})
    line = report.lines[0]
    assert line.verdict == reconcile.UNCOVERED
    assert "no opinion" in line.why


def test_a_universe_name_that_failed_the_screen_says_so():
    class _S:
        eligible = False
        total = 40.0
    report = reconcile.build(
        [{"symbol": "AAPL", "qty": 1, "market_value": 100}],
        _Basket([]), {"AAPL": _S()})
    assert report.lines[0].verdict == reconcile.REJECTED
    assert "not a sell instruction" in report.lines[0].why


def test_a_basket_name_you_do_not_hold_is_flagged_missing():
    # Sized close to target on purpose. A single holding at 100% of a
    # portfolio against a 5% target is genuinely oversized, and reporting it
    # as a keep would be the bug rather than the fix.
    report = reconcile.build(
        [{"symbol": "AAPL", "qty": 1, "market_value": 50},
         {"symbol": "VTI", "qty": 1, "market_value": 950}],
        _Basket([_Pick("AAPL", 0.05), _Pick("VTI", 0.90), _Pick("GLD", 0.04)]),
        {})
    verdicts = {l.symbol: l.verdict for l in report.lines}
    assert verdicts["GLD"] == reconcile.ADD
    assert verdicts["AAPL"] == reconcile.KEEP


def test_small_drift_does_not_become_a_trim():
    """Trimming for two points of drift is churn, which this system avoids."""
    report = reconcile.build(
        [{"symbol": "AAPL", "qty": 1, "market_value": 106},
         {"symbol": "GLD", "qty": 1, "market_value": 894}],
        _Basket([_Pick("AAPL", 0.10), _Pick("GLD", 0.90)]), {})
    assert {l.symbol: l.verdict for l in report.lines}["AAPL"] == reconcile.KEEP


def test_a_genuinely_oversized_holding_is_flagged():
    report = reconcile.build(
        [{"symbol": "AAPL", "qty": 1, "market_value": 800},
         {"symbol": "GLD", "qty": 1, "market_value": 200}],
        _Basket([_Pick("AAPL", 0.10), _Pick("GLD", 0.20)]), {})
    assert {l.symbol: l.verdict for l in report.lines}["AAPL"] == reconcile.TRIM
