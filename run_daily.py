#!/usr/bin/env python3
"""
The single orchestrator. One run, one snapshot, one decision memo.

    python run_daily.py                  # normal daily run
    python run_daily.py --replay 2026-08-26   # re-run an old snapshot
    python run_daily.py --no-llm         # deterministic text only
    python run_daily.py --dry-run        # compute everything, save nothing

It stops after writing the memo. Nothing is sent to a broker here. Orders leave
this system only when a human approves a proposal in the dashboard, which calls
api/main.py, which calls the broker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from src import (allocate, collect, config, ingest, llm, report, risk,  # noqa: E402
                 score)
from src.brokers.registry import Registry  # noqa: E402
from src.state import PENDING, Store, proposal_id  # noqa: E402

log = logging.getLogger("run_daily")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Once-daily AI portfolio manager")
    ap.add_argument("--replay", metavar="SNAPSHOT_ID",
                    help="re-run a saved snapshot instead of fetching a new one")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip Claude, use deterministic reasoning text")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, write nothing to disk")
    ap.add_argument("--mock", action="store_true",
                    help="force the mock broker even if Alpaca keys are present")
    ap.add_argument("--run-id", help="override the run id (defaults to today)")
    ap.add_argument("--tag", default="",
                    help="which scheduled slot this is: open, close, overnight")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    store = Store()
    registry = Registry(force_mock=args.mock)

    # 1. snapshot ---------------------------------------------------------
    if args.replay:
        snapshot = store.get_snapshot(args.replay)
        if snapshot is None:
            log.error("no snapshot %s in data/snapshots/", args.replay)
            return 2
        log.info("replaying snapshot %s", args.replay)
    else:
        log.info("building snapshot ...")
        snapshot = collect.build(registry)
        if not args.dry_run:
            store.save_snapshot(snapshot["snapshot_id"], snapshot)
        log.info("snapshot %s: %d symbols, %.0f%% coverage, equity $%s",
                 snapshot["snapshot_id"], len(snapshot["symbols"]),
                 snapshot["coverage"] * 100,
                 f"{snapshot['portfolio']['equity']:,.2f}")

    for err in snapshot.get("errors", []):
        log.warning("collection: %s", err)

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    # Three scheduled runs a day share a date, so the tag has to be part of the
    # id or the later ones silently overwrite the earlier ones.
    run_id = args.run_id or (f"{today}-{args.tag}" if args.tag else today)

    # 2. score ------------------------------------------------------------
    from src.data import fundamentals as fundamentals_mod
    funds = fundamentals_mod.fetch(snapshot["symbols"])
    log.info("fundamentals: %d of %d symbols have usable data",
             sum(1 for f in funds.values() if f.usable), len(funds))

    # Sentiment over the last few days of headlines. Cached, so a scheduled run
    # rarely pays for it, and left UNSCORED wherever there is too little news
    # rather than filled with a neutral that looks like a reading.
    senti = _sentiment_for(snapshot["symbols"])
    log.info("sentiment: %d of %d symbols scored",
             sum(1 for v in senti.values() if v.usable), len(senti))

    scores = score.score_universe(snapshot["bars"], fundamentals=funds,
                                  sentiment=senti)
    eligible = [s for s in scores.values() if s.eligible]
    log.info("scored %d symbols, %d above threshold", len(scores), len(eligible))

    # 3. allocate ---------------------------------------------------------
    positions = _positions_from(snapshot)
    equity = snapshot["portfolio"]["equity"]
    proposals = allocate.build(scores, positions, equity)
    changes = [p for p in proposals if p.action != "KEEP"]
    log.info("allocator produced %d changes out of %d names",
             len(changes), len(proposals))

    proposal_dicts = [p.to_dict() for p in proposals]

    # 3b. the committee's only influence ----------------------------------
    #
    # Recent verdicts scale a weight the allocator already chose, inside the
    # band in config/committee.yaml. The committee cannot introduce a name,
    # remove one, or widen a limit, and the risk gate below still runs on
    # whatever comes out. A verdict older than the configured window is
    # ignored rather than applied faintly.
    _apply_committee(proposal_dicts, store)

    # 4. reasoning --------------------------------------------------------
    if args.no_llm:
        annotations, llm_source = (
            {p["symbol"]: llm._deterministic_annotation(p) for p in proposal_dicts
             if p["action"] != "KEEP"},
            "deterministic",
        )
    else:
        annotations, llm_source = llm.analyst_pass(
            proposal_dicts, snapshot, store.theses()
        )
    log.info("reasoning source: %s", llm_source)

    for p in proposal_dicts:
        ann = annotations.get(p["symbol"])
        if ann:
            p.update({k: v for k, v in ann.items() if k != "source"})

    # 5. risk gate --------------------------------------------------------
    account = _account_from(snapshot)
    quotes = _quotes_from(snapshot)
    broker_mode = next(
        (h["mode"] for h in snapshot.get("broker_health", []) if h["ok"]), "paper"
    )
    actionable = [p for p in proposal_dicts if p["action"] != "KEEP"]
    verdict = risk.evaluate(
        actionable, account=account, quotes=quotes, broker_mode=broker_mode,
        # Scheduled rebalancing may not break the minimum holding period. Only
        # the event path, on a genuinely invalidated thesis, can do that.
        held_days={p["symbol"]: store.held_days(p["symbol"]) for p in actionable},
    )
    log.info("risk gate: %s (%s), %d blocked",
             "PASS" if verdict.passed else "FAIL",
             verdict.to_dict()["summary"], len(verdict.blocked))

    # 6. assemble the run record -----------------------------------------
    for p in proposal_dicts:
        p["id"] = proposal_id(run_id, p["symbol"])
        if p["action"] == "KEEP":
            p["status"] = "no_action"
        elif p["symbol"] in verdict.blocked:
            p["status"] = "blocked"
            p["decision_note"] = verdict.blocked[p["symbol"]]
        elif not verdict.passed:
            p["status"] = "blocked"
            p["decision_note"] = "portfolio-level risk failure, see checks"
        else:
            p["status"] = PENDING
        p["decided_at"] = None
        p["order"] = None

    run: dict[str, Any] = {
        "run_id": run_id,
        "kind": "scheduled",
        "tag": args.tag or "manual",
        "snapshot_id": snapshot["snapshot_id"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "replayed": bool(args.replay),
        "llm_source": llm_source,
        "portfolio": snapshot["portfolio"],
        "benchmark": snapshot.get("benchmark"),
        "benchmark_return": None,
        "routing": snapshot.get("routing", {}),
        "broker_health": snapshot.get("broker_health", []),
        "broker_failures": snapshot.get("broker_failures", {}),
        "config_versions": snapshot.get("config_versions", {}),
        "coverage": snapshot.get("coverage"),
        "missing_data": snapshot.get("missing_data", []),
        "errors": snapshot.get("errors", []),
        "proposals": proposal_dicts,
        "risk": verdict.to_dict(),
        "scores": {s: sc.to_dict() for s, sc in scores.items()},
        "fundamentals": {s: f.to_dict() for s, f in funds.items()},
        "sentiment": {s: v.to_dict() for s, v in senti.items()},
    }
    run["benchmark_return"] = _benchmark_return(snapshot, store)

    # Concentration and the allocation look back are portfolio-level views the
    # per-name limits cannot produce.
    from src import portfolio_risk
    held_weights = {p["symbol"].upper(): p["weight"]
                    for p in snapshot["portfolio"]["positions"]}
    if held_weights:
        run["concentration"] = portfolio_risk.analyse(
            held_weights, snapshot["bars"]).to_dict()
        bt = portfolio_risk.backtest_weights(
            held_weights, snapshot["bars"], snapshot.get("benchmark") or "SPY",
            starting_equity=equity)
        run["allocation_backtest"] = bt.to_dict() if bt else None
        if bt:
            log.info("allocation look back: %+.1f%% vs benchmark %+.1f%%, "
                     "max drawdown %.1f%%",
                     bt.total_return * 100, bt.benchmark_return * 100,
                     bt.max_drawdown * 100)
        if run.get("concentration"):
            log.info("concentration: %s", run["concentration"]["note"])

    # 7. narrative --------------------------------------------------------
    run["summary"], _ = (
        (llm._deterministic_summary(run), "deterministic") if args.no_llm
        else llm.write_summary(run)
    )
    run["residual_risk"] = _residual_risk(run, args.no_llm)

    # 8. persist ----------------------------------------------------------
    if args.dry_run:
        print(report.render(run, store))
        log.info("dry run, nothing written")
        return 0

    store.save_run(run)

    # Start the minimum holding clock for anything held but never recorded.
    # Without this, a position opened before thesis tracking existed has no
    # open date and silently skips the holding period check forever.
    for pos in snapshot["portfolio"]["positions"]:
        sym = pos["symbol"].upper()
        if store.opened_on(sym) is None:
            prior = store.prior_thesis(sym)
            store.remember_thesis(sym, {
                **prior,
                "thesis": prior.get("thesis", "position predates thesis tracking"),
                "opened_on": dt.datetime.now(dt.timezone.utc).date().isoformat(),
            })
    for p in proposal_dicts:
        if p["action"] != "KEEP" and p.get("thesis"):
            # opened_on is what the minimum holding period counts from, so it
            # is set once when a position first opens and never moved after.
            existing = store.opened_on(p["symbol"])
            opened_on = existing or (
                dt.datetime.now(dt.timezone.utc).date() if p["action"] == "OPEN" else None
            )
            store.remember_thesis(p["symbol"], {
                "thesis": p["thesis"], "action": p["action"],
                "target_weight": p["target_weight"], "run_id": run_id,
                "exit_rule": p.get("exit_rule", ""),
                "invalidation": p.get("invalidation", ""),
                "opened_on": opened_on.isoformat() if opened_on else None,
            })
    path = report.save(run, store)

    # The memo is the only record of what this system believed today and why,
    # so it goes into retrieval where a later committee can find it.
    ingest.memo(run["run_id"], run.get("summary", ""))
    ingest.prune()

    pending = sum(1 for p in proposal_dicts if p["status"] == PENDING)
    log.info("memo written to %s", path)
    log.info("%d proposals awaiting approval in the dashboard", pending)
    return 0


# --------------------------------------------------------------------------
# small adapters so the pipeline can run off a replayed snapshot with no broker
# --------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _positions_from(snapshot: dict[str, Any]) -> list[Any]:
    return [
        _Obj(symbol=p["symbol"], market_value=p["market_value"], qty=p["qty"],
             avg_cost=p["avg_cost"])
        for p in snapshot["portfolio"]["positions"]
    ]


def _account_from(snapshot: dict[str, Any]) -> Any:
    p = snapshot["portfolio"]
    return _Obj(equity=p["equity"], cash=p["cash"], buying_power=p["cash"])


def _quotes_from(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        sym: _Obj(price=q["price"], age_hours=q.get("age_hours", 0.0),
                  stale=q.get("stale", False))
        for sym, q in snapshot.get("quotes", {}).items()
    }


def _sentiment_for(symbols: list[str]) -> dict[str, Any]:
    """Pull recent headlines and score how the coverage reads, per symbol."""
    import datetime as _dt
    from src import sentiment as sentiment_mod
    from src.data import news as newsmod

    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
    items, provider = newsmod.fetch_all(symbols, since, limit=120)
    if provider == "none" or not items:
        log.info("sentiment: no news provider, every symbol stays unscored")
        return {}
    # The same headlines feed retrieval. Fetched once, used twice.
    added = ingest.news(items)
    if added:
        log.info("indexed %d news passages for retrieval", added)
    return sentiment_mod.score_symbols(sentiment_mod.group_by_symbol(items))


def _benchmark_return(snapshot: dict[str, Any], store: Store) -> float | None:
    """Buy the benchmark on the first recorded run day and hold. No trading."""
    bench = (snapshot.get("benchmark") or "").upper()
    bars = snapshot.get("bars", {}).get(bench) or []
    runs = sorted(store.list_runs(limit=400))
    if not bars or not runs:
        return None
    start_day = runs[0]
    start_close = next(
        (b["close"] for b in bars if b["t"][:10] >= start_day), None
    )
    if not start_close:
        return None
    return round(bars[-1]["close"] / start_close - 1.0, 6)


def _residual_risk(run: dict[str, Any], no_llm: bool) -> str:
    if no_llm or not llm.available():
        return ""
    payload = {
        "cash_weight": run["portfolio"]["cash_weight"],
        "targets": [
            {"symbol": p["symbol"], "asset_class": p["asset_class"],
             "target_weight": p["target_weight"]}
            for p in run["proposals"] if p["target_weight"] > 0
        ],
        "checks": run["risk"]["checks"],
        "blocked": run["risk"]["blocked"],
    }
    try:
        return llm._complete(
            llm.load_prompt("risk_review"),
            json.dumps(payload, indent=2, default=str),
            max_tokens=1200,
        ).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("residual risk note skipped: %s", e)
        return ""


if __name__ == "__main__":
    sys.exit(main())


def _apply_committee(proposals: list[dict[str, Any]], store: Store,
                     max_age_days: int = 3) -> None:
    """Scale target weights by recent committee multipliers, then re-clamp."""
    cap = float(config.risk()["limits"]["max_single_position"])
    today = dt.date.today()

    for p in proposals:
        saved = store.committee(p["symbol"])
        if not saved:
            continue
        try:
            seen = dt.datetime.fromisoformat(saved.get("at", "")).date()
        except ValueError:
            continue
        age = (today - seen).days
        if age > max_age_days:
            continue

        multiplier = float(saved.get("multiplier", 1.0))
        if abs(multiplier - 1.0) < 1e-9:
            continue

        before = float(p.get("target_weight", 0.0))
        # Clamped again after scaling, because a 1.2x on a position already at
        # the cap would otherwise walk straight through it.
        after = min(before * multiplier, cap)
        if abs(after - before) < 1e-6:
            continue

        p["target_weight"] = round(after, 6)
        p["committee"] = {
            "consensus": saved.get("consensus"),
            "score": saved.get("score"),
            "disagreement": saved.get("disagreement"),
            "multiplier": multiplier,
            "as_of": saved.get("at"),
            "age_days": age,
            "weight_before": round(before, 6),
        }
        log.info("committee %s %s: %.2f%% -> %.2f%% (%.3fx)",
                 p["symbol"], saved.get("consensus"),
                 before * 100, after * 100, multiplier)
