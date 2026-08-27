#!/usr/bin/env python3
"""
The event path.

Separate from run_daily.py on purpose. The scheduled run reviews the whole
portfolio and always waits for a human. This one wakes on a single headline,
re-reads one thesis, and may act by itself when autopilot is armed.

    python run_event.py                 # check news, assess, act or queue
    python run_event.py --dry-run       # assess and print, change nothing
    python run_event.py --since 6h      # look further back than the last check
    python run_event.py --force-review  # ignore the seen ledger

Guardrails, in the order they apply:

  autopilot armed          off by default, toggled from the dashboard
  paper account only       refused on a live broker, checked in code
  material and confident   both thresholds from risk.yaml must clear
  Kelly sizing             quarter Kelly, capped at max_single_position
  the same risk gate       every limit the scheduled run obeys
  per symbol cooldown      one story cannot fire repeatedly
  daily trade cap          a bad news day cannot rewrite the portfolio

Anything that fails a guardrail becomes a pending proposal in the dashboard
rather than being dropped, so a blocked event is still visible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from src import config, events, kelly, risk  # noqa: E402
from src.brokers.base import BrokerError, OrderRequest  # noqa: E402
from src.brokers.registry import Registry  # noqa: E402
from src.data import news as newsmod  # noqa: E402
from src.state import EXECUTED, FAILED, PENDING, Store, proposal_id  # noqa: E402

log = logging.getLogger("run_event")

DURATION = re.compile(r"^(\d+)([mhd])$")


def parse_since(text: str) -> dt.timedelta:
    m = DURATION.match(text.strip().lower())
    if not m:
        raise ValueError(f"cannot read duration {text!r}, use forms like 30m, 6h, 2d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n),
            "d": dt.timedelta(days=n)}[unit]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Event-driven portfolio review")
    ap.add_argument("--since", default=None, help="look back this far, e.g. 6h")
    ap.add_argument("--dry-run", action="store_true", help="assess only, change nothing")
    ap.add_argument("--force-review", action="store_true",
                    help="reassess items already in the seen ledger")
    ap.add_argument("--symbols", help="comma separated, defaults to holdings plus universe")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    store = Store()
    reg = Registry()
    cfg = config.risk()
    auto_cfg = cfg.get("autopilot", {})

    # ---- what to watch --------------------------------------------------
    try:
        positions = {p.symbol.upper(): p for b in reg.active().values()
                     for p in b.get_positions()}
        equity = sum(b.get_account().equity for b in reg.active().values())
    except BrokerError as e:
        log.error("broker unreachable: %s", e)
        return 2

    if args.symbols:
        watch = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        # Holdings first: news about something you own matters more than news
        # about something you might buy.
        watch = list(dict.fromkeys(list(positions) + config.all_symbols()))

    since = (dt.datetime.now(dt.timezone.utc) - parse_since(args.since)) if args.since \
        else (store.last_event_check() or
              dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6))

    # ---- fetch and shortlist -------------------------------------------
    items, provider = newsmod.fetch_all(watch, since, limit=60)
    log.info("news provider %s returned %d items since %s",
             provider, len(items), since.strftime("%Y-%m-%d %H:%M"))
    if provider == "none":
        log.warning("no news provider configured. Set ALPACA_KEY_ID and "
                    "ALPACA_SECRET_KEY, or FIRECRAWL_API_KEY.")

    seen = set() if args.force_review else store.seen_news()
    candidates = events.shortlist(items, seen=seen)
    log.info("%d items cleared the prefilter", len(candidates))
    for item, score in candidates[:10]:
        log.info("  %.2f  %-8s %s", score, item.symbol, item.headline[:88])

    if not candidates:
        if not args.dry_run:
            store.set_last_event_check()
            store.mark_seen([i.id for i in items])
        log.info("nothing material, no action")
        return 0

    # ---- classify -------------------------------------------------------
    pos_dicts = {
        s: {"weight": p.market_value / equity if equity else 0.0,
            "unrealized_plpc": p.unrealized_plpc}
        for s, p in positions.items()
    }
    assessments = events.classify(candidates, pos_dicts, store.theses())

    armed = store.autopilot() and bool(auto_cfg.get("enabled", False))
    log.info("autopilot %s", "ARMED" if armed else "disarmed")

    acted: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []

    for a in assessments:
        decision = _decide(a, positions, equity, store, cfg, reg, armed=armed,
                           dry_run=args.dry_run)
        (acted if decision.get("executed") else queued).append(decision)
        log.info("%-8s %-8s materiality=%.2f conf=%.2f -> %s",
                 a.symbol, a.action, a.materiality, a.confidence,
                 decision["outcome"])

    # ---- persist --------------------------------------------------------
    if args.dry_run:
        log.info("dry run, nothing written")
        return 0

    run_id = f"event-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H%M%S')}"
    store.save_run({
        "run_id": run_id,
        "kind": "event",
        "snapshot_id": None,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "news_provider": provider,
        "autopilot_armed": armed,
        "portfolio": {"equity": round(equity, 2), "cash": 0.0, "cash_weight": 0.0,
                      "positions": []},
        "assessments": [a.to_dict() for a in assessments],
        "proposals": [d["proposal"] for d in (acted + queued) if d.get("proposal")],
        "risk": {"passed": True, "checks": [], "blocked": {}, "summary": "per event"},
        "summary": _summarise(assessments, acted, queued, provider, armed),
    })
    store.mark_seen([i.id for i in items])
    store.set_last_event_check()

    log.info("%d executed, %d queued for review, record %s",
             len(acted), len(queued), run_id)
    return 0


# --------------------------------------------------------------------------

def _decide(a, positions, equity, store, cfg, reg, *, armed: bool,
            dry_run: bool) -> dict[str, Any]:
    """Size one assessment, gate it, and either act or queue it."""
    auto = cfg.get("autopilot", {})
    sym = a.symbol
    current_weight = (positions[sym].market_value / equity
                      if sym in positions and equity else 0.0)

    if a.action == "HOLD":
        return {"outcome": "hold, no change", "executed": False, "proposal": None}

    # Kelly is for entries. An exit is a risk action and is not odds-sized.
    if a.action in ("EXIT", "REDUCE"):
        target = 0.0 if a.action == "EXIT" else round(current_weight * 0.5, 6)
        sizing = kelly.Sizing(True, target, "risk action, not Kelly sized")
    else:
        sizing = kelly.size(a.probability, a.upside, a.downside,
                            current_weight=current_weight, risk_cfg=cfg)
        target = sizing.weight

    proposal = {
        "id": proposal_id(
            f"event-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}", sym),
        "symbol": sym,
        "asset_class": config.class_of(sym),
        "action": a.action,
        "current_weight": round(current_weight, 6),
        "target_weight": round(target, 6),
        "delta_weight": round(target - current_weight, 6),
        "delta_usd": round((target - current_weight) * equity, 2),
        "score": round(a.materiality * 100, 1),
        "score_components": {"materiality": round(a.materiality * 100, 1),
                             "confidence": round(a.confidence * 100, 1)},
        "unscored": [],
        "thesis": a.rationale,
        "counter": a.counter,
        "exit_rule": a.exit_rule,
        "invalidation": a.invalidation,
        "confidence": _confidence_label(a.confidence),
        "role": "event driven",
        "event": {
            "headline": a.headline, "url": a.url, "published_at": a.published_at,
            "direction": a.direction, "thesis_impact": a.thesis_impact,
            "materiality": a.materiality, "classifier": a.source,
        },
        "kelly": sizing.to_dict(),
        # A genuinely invalidated thesis is what the holding period yields to.
        "override_holding_period": a.thesis_impact == "invalidates",
        "status": PENDING,
        "decided_at": None,
        "order": None,
    }

    reasons: list[str] = []
    if not a.material:
        reasons.append("classifier did not judge this material")
    if a.materiality < float(auto.get("min_materiality", 0.7)):
        reasons.append(f"materiality {a.materiality:.2f} below "
                       f"{float(auto.get('min_materiality', 0.7)):.2f}")
    if a.confidence < float(auto.get("min_confidence", 0.6)):
        reasons.append(f"confidence {a.confidence:.2f} below "
                       f"{float(auto.get('min_confidence', 0.6)):.2f}")
    if not sizing.ok:
        reasons.append(sizing.reason)
    if a.action in ("OPEN", "INCREASE") and not auto.get("allow_auto_entry", True):
        reasons.append("automatic entries are switched off")
    if a.action in ("EXIT", "REDUCE") and not auto.get("allow_auto_exit", True):
        reasons.append("automatic exits are switched off")
    if store.cooling_down(sym, int(auto.get("cooldown_minutes", 90))):
        reasons.append("inside the per symbol cooldown")
    if store.auto_trades_today() >= int(auto.get("max_trades_per_day", 3)):
        reasons.append("daily automatic trade cap reached")

    # max_notional_per_trade_pct is a size limit, not a veto. Kelly sizes the
    # target; if reaching it in one go would breach the cap, take the position
    # part of the way there and let the next event or the scheduled run
    # continue. Refusing outright would block almost every entry, since Kelly
    # targets are routinely larger than one automatic order is allowed to be.
    cap = float(auto.get("max_notional_per_trade_pct", 0.05))
    if abs(proposal["delta_weight"]) > cap:
        direction = 1.0 if proposal["delta_weight"] > 0 else -1.0
        clamped = round(current_weight + direction * cap, 6)
        proposal["kelly"]["uncapped_target"] = proposal["target_weight"]
        proposal["target_weight"] = max(0.0, clamped)
        proposal["delta_weight"] = round(proposal["target_weight"] - current_weight, 6)
        proposal["delta_usd"] = round(proposal["delta_weight"] * equity, 2)
        proposal["kelly"]["reason"] += (
            f"; order clamped to {cap:.0%} of equity by the automatic per trade cap, "
            f"so this is a partial move toward the {sizing.weight:.1%} target"
        )

    broker = reg.for_symbol(sym)
    if broker.mode != "paper" or not auto.get("paper_only", True):
        reasons.append("automatic execution is refused on a live account")

    # The same deterministic gate the scheduled run uses.
    try:
        account = broker.get_account()
        quotes = broker.get_quotes([sym])
    except BrokerError as e:
        reasons.append(f"broker unreachable: {e}")
        account, quotes = None, None

    if account is not None:
        verdict = risk.evaluate([proposal], account=account, quotes=quotes,
                                broker_mode=broker.mode,
                                held_days={sym: store.held_days(sym)})
        proposal["risk"] = verdict.to_dict()
        ok, why = risk.approvable(proposal, verdict)
        if not ok:
            reasons.append(why)

    if reasons or not armed:
        if not armed:
            reasons.insert(0, "autopilot disarmed")
        proposal["decision_note"] = "; ".join(reasons)
        return {"outcome": f"queued for review ({reasons[0]})",
                "executed": False, "proposal": proposal}

    if dry_run:
        return {"outcome": "would execute (dry run)", "executed": False,
                "proposal": proposal}

    # ---- execute --------------------------------------------------------
    delta_usd = proposal["delta_usd"]
    if abs(delta_usd) < 1.0:
        proposal["status"] = EXECUTED
        proposal["decision_note"] = "already at target, no order needed"
        return {"outcome": "already at target", "executed": False, "proposal": proposal}

    order = OrderRequest(
        symbol=sym, side="buy" if delta_usd > 0 else "sell",
        notional=abs(round(delta_usd, 2)), asset_class=proposal["asset_class"],
        client_order_id=f"pm-evt-{proposal['id'].replace(':', '-')}"[:48],
    )
    try:
        result = broker.submit_order(order)
    except BrokerError as e:
        proposal["status"] = FAILED
        proposal["decision_note"] = str(e)
        store.log_execution({"event": "auto_failed", "symbol": sym, "error": str(e),
                             "headline": a.headline})
        return {"outcome": f"broker rejected: {e}", "executed": False,
                "proposal": proposal}

    proposal["status"] = EXECUTED
    proposal["order"] = result.to_dict()
    proposal["decided_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    proposal["decision_note"] = "executed automatically on a material event"
    store.record_auto_trade(sym)
    store.log_execution({
        "event": "auto_executed", "symbol": sym, "side": order.side,
        "notional": abs(round(delta_usd, 2)), "action": a.action,
        "headline": a.headline, "url": a.url,
        "materiality": a.materiality, "confidence": a.confidence,
        "kelly": sizing.to_dict(), "order": result.to_dict(), "broker": broker.name,
    })
    if a.rationale:
        store.remember_thesis(sym, {
            "thesis": a.rationale, "action": a.action,
            "target_weight": proposal["target_weight"],
            "exit_rule": a.exit_rule, "invalidation": a.invalidation,
            "opened_on": (store.opened_on(sym) or
                          dt.datetime.now(dt.timezone.utc).date()).isoformat(),
            "source": "event",
        })
    return {"outcome": f"EXECUTED {order.side} ${abs(delta_usd):,.2f}",
            "executed": True, "proposal": proposal}


def _confidence_label(v: float) -> str:
    return "high" if v >= 0.75 else "medium" if v >= 0.5 else "low"


def _summarise(assessments, acted, queued, provider, armed) -> str:
    material = [a for a in assessments if a.material]
    if not assessments:
        return "No headlines cleared the prefilter."
    parts = [
        f"{len(assessments)} headlines assessed via {provider}, "
        f"{len(material)} judged material."
    ]
    if acted:
        parts.append("Executed automatically: " + "; ".join(
            f"{d['proposal']['action']} {d['proposal']['symbol']}" for d in acted) + ".")
    if queued:
        parts.append(f"{len(queued)} waiting for review in the dashboard.")
    if not armed:
        parts.append("Autopilot is disarmed, so nothing could execute on its own.")
    return " ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
