"""
The daily memo.

The report is the product. If it is clear enough, you can run this system
without reading the code: what it holds, what it wants to change, which limits
it checked, what it could not score, and what would change its mind tomorrow.

Every section is generated from the saved run record, so a memo can always be
regenerated from disk without re-running the pipeline.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .state import Store

ACTION_ORDER = {"EXIT": 0, "REDUCE": 1, "OPEN": 2, "INCREASE": 3, "KEEP": 4}


def render(run: dict[str, Any], store: Store | None = None) -> str:
    store = store or Store()
    p = run["portfolio"]
    risk = run["risk"]
    changes = [x for x in run["proposals"] if x["action"] != "KEEP"]

    out: list[str] = []
    add = out.append

    add(f"# Daily portfolio review, {run['run_id']}")
    add("")
    add(f"Snapshot `{run['snapshot_id']}`, generated {run['generated_at']}. "
        f"Reasoning written by {run.get('llm_source', 'deterministic')}.")
    add("")

    # 1 -------------------------------------------------------------
    add("## Executive summary")
    add("")
    add(run.get("summary", "").strip() or "No summary was produced for this run.")
    add("")

    # 2 -------------------------------------------------------------
    add("## Portfolio state")
    add("")
    add(f"Equity ${p['equity']:,.2f}. Cash ${p['cash']:,.2f} ({p['cash_weight']:.1%}). "
        f"{len(p['positions'])} open positions.")
    add("")
    if p["positions"]:
        add("| Symbol | Class | Weight | Qty | Avg cost | Market value | Unrealised |")
        add("|---|---|---:|---:|---:|---:|---:|")
        for pos in sorted(p["positions"], key=lambda x: -x["market_value"]):
            add(f"| {pos['symbol']} | {pos.get('universe_class', '?')} | "
                f"{pos['weight']:.1%} | {pos['qty']:.4f} | ${pos['avg_cost']:,.2f} | "
                f"${pos['market_value']:,.2f} | {pos['unrealized_plpc']:+.2%} |")
    else:
        add("No open positions. The account is entirely in cash.")
    add("")

    by_class = _class_exposure(run)
    if by_class:
        add("Exposure by class: " + ", ".join(
            f"{cls} {w:.1%}" for cls, w in sorted(by_class.items(), key=lambda kv: -kv[1])
        ) + f", cash {p['cash_weight']:.1%}.")
        add("")

    # 3 -------------------------------------------------------------
    add("## Benchmark")
    add("")
    add(_benchmark_block(run, store))
    add("")

    # 4 -------------------------------------------------------------
    add("## Ranked opportunities")
    add("")
    scores = run.get("scores", {})
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["total"])[:15]
    add("| Symbol | Score | Valuation | Quality | Catalyst | Momentum | Vol | Measured |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for sym, v in ranked:
        c = v.get("components", {})
        if not c:
            continue
        add(f"| {sym} | {v['total']:.1f} | "
            f"{c.get('valuation', 0):.0f} | {c.get('quality', 0):.0f} | "
            f"{c.get('catalyst', 0):.0f} | {c.get('momentum', 0):.0f} | "
            f"{c.get('volatility', 0):.0f} | {v.get('coverage', 1.0):.0%} |")
    add("")
    add("Measured is the share of the rubric backed by real data for that name. "
        "A component with no source is filled with the neutral value and "
        "excluded from the weighting, so a thin score is visible as one rather "
        "than being dragged toward the middle.")
    add("")

    funds = run.get("fundamentals", {})
    usable = [f for f in funds.values() if f.get("quote_type") == "EQUITY" and not f.get("error")]
    if usable:
        add(f"Fundamentals covered {len(usable)} operating companies. ETFs and "
            f"crypto carry no earnings or margins, so valuation, quality and "
            f"catalyst stay unscored for them rather than being invented.")
        add("")


    conc = run.get("concentration")
    if conc and conc.get("nominal_n"):
        add("## Concentration")
        add("")
        add(conc.get("note", ""))
        add("")
        if conc.get("effective_n"):
            add(f"Nominal positions {conc['nominal_n']}, effective independent "
                f"bets {conc['effective_n']:.1f}. Portfolio volatility "
                f"{conc.get('portfolio_vol', 0):.1%}, diversification ratio "
                f"{conc.get('diversification_ratio', 1):.2f}.")
            add("")
        if conc.get("largest_cluster") and len(conc["largest_cluster"]) > 1:
            add(f"Correlated cluster: {', '.join(conc['largest_cluster'])} at "
                f"{conc['largest_cluster_weight']:.0%} of the book. The class "
                f"limits count these separately; the correlation does not.")
            add("")

    bt = run.get("allocation_backtest")
    if bt:
        add("## Allocation look back")
        add("")
        add(bt.get("note", ""))
        add("")
        add(f"Volatility {bt.get('volatility', 0):.1%}, Sharpe "
            f"{bt.get('sharpe', 0):.2f}, worst drawdown "
            f"{bt.get('max_drawdown', 0):.1%}.")
        add("")

    # 5 -------------------------------------------------------------
    add("## Risk checkpoint")
    add("")
    add(f"{risk['summary']}. Gate {'PASSED' if risk['passed'] else 'FAILED'}.")
    add("")
    add("| Rule | Scope | Result | Actual | Limit |")
    add("|---|---|---|---:|---:|")
    for c in risk["checks"]:
        mark = "PASS" if c["passed"] else ("WARN" if c["severity"] == "warn" else "FAIL")
        add(f"| {c['rule']} | {c['scope']} | {mark} | {c['actual']} | {c['limit']} |")
    add("")
    if risk["blocked"]:
        add("Blocked by the gate:")
        add("")
        for sym, why in risk["blocked"].items():
            add(f"- {sym}: {why}")
        add("")
    if run.get("residual_risk"):
        add("Residual risk after all hard rules passed:")
        add("")
        add(run["residual_risk"].strip())
        add("")

    # 6 -------------------------------------------------------------
    add("## Proposed changes")
    add("")
    if not changes:
        add("None. Every holding is inside the rebalance band and no watchlist "
            "name crossed the score threshold. This was a monitoring run.")
        add("")
    else:
        add("| Symbol | Action | Before | After | Change | Value | Score | Confidence |")
        add("|---|---|---:|---:|---:|---:|---:|---|")
        for x in sorted(changes, key=lambda y: (ACTION_ORDER.get(y["action"], 9), -abs(y["delta_weight"]))):
            add(f"| {x['symbol']} | {x['action']} | {x['current_weight']:.1%} | "
                f"{x['target_weight']:.1%} | {x['delta_weight']:+.1%} | "
                f"${x['delta_usd']:+,.2f} | {x['score']:.1f} | {x.get('confidence', 'unrated')} |")
        add("")
        for x in sorted(changes, key=lambda y: ACTION_ORDER.get(y["action"], 9)):
            add(f"### {x['action']} {x['symbol']}")
            add("")
            add(f"{x['current_weight']:.1%} to {x['target_weight']:.1%} "
                f"(${x['delta_usd']:+,.2f}). Role: {x.get('role') or 'unstated'}.")
            add("")
            if x.get("thesis"):
                add(f"Thesis. {x['thesis']}")
                add("")
            if x.get("counter"):
                add(f"Against. {x['counter']}")
                add("")
            if x.get("exit_rule"):
                add(f"Exit rule. {x['exit_rule']}")
                add("")
            if x.get("invalidation"):
                add(f"Invalidated if. {x['invalidation']}")
                add("")

    # 7 -------------------------------------------------------------
    add("## What needs approval")
    add("")
    pending = [x for x in run["proposals"] if x.get("status") == "pending"]
    if not pending:
        add("Nothing. No order will be sent.")
    else:
        add(f"{len(pending)} proposals are waiting in the dashboard. Nothing reaches "
            f"the broker until each one is approved individually.")
        add("")
        for x in pending:
            add(f"- {x['action']} {x['symbol']}, ${x['delta_usd']:+,.2f}")
    add("")

    # 8 -------------------------------------------------------------
    add("## What would change the view tomorrow")
    add("")
    invalidations = [(x["symbol"], x["invalidation"]) for x in changes if x.get("invalidation")]
    if invalidations:
        for sym, inv in invalidations:
            add(f"- {sym}: {inv}")
    else:
        add("- No open proposals carry an invalidation condition this run.")
    if run.get("missing_data"):
        add(f"- Price history is missing for {', '.join(run['missing_data'])}. "
            f"If it arrives, those names become scoreable.")
    add("")

    # 9 -------------------------------------------------------------
    add("## Audit appendix")
    add("")
    add(f"- Snapshot: `data/snapshots/{run['snapshot_id']}.json`")
    add(f"- Run record: `data/runs/{run['run_id']}.json`")
    add(f"- Config versions: {run.get('config_versions', {})}")
    add(f"- Routing: {run.get('routing', {})}")
    for h in run.get("broker_health", []):
        add(f"- Broker {h['name']} ({h['mode']}): {'ok' if h['ok'] else 'DOWN'}, {h['detail']}")
    if run.get("broker_failures"):
        for name, why in run["broker_failures"].items():
            add(f"- Broker {name} unavailable: {why}")
    add(f"- Data coverage: {run.get('coverage', 0):.1%} of the universe had usable bars")
    if run.get("errors"):
        for e in run["errors"]:
            add(f"- Collection error: {e}")
    add("")
    add("Research and decision support only. Not financial advice. "
        "Paper account, simulated orders.")
    add("")
    return "\n".join(out)


# --------------------------------------------------------------------------

def _class_exposure(run: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in run["portfolio"]["positions"]:
        cls = pos.get("universe_class", "unknown")
        out[cls] = out.get(cls, 0.0) + pos.get("weight", 0.0)
    return out


def _benchmark_block(run: dict[str, Any], store: Store) -> str:
    """
    The do-nothing comparison. A portfolio system that cannot beat holding the
    benchmark is worth knowing about on day one, not after a year.
    """
    bench = run.get("benchmark")
    if not bench:
        return "No benchmark is configured in universe.yaml."

    history = _equity_history(store)
    if len(history) < 2:
        return (f"Benchmark is {bench}. Not enough run history yet to compare. "
                f"The comparison starts once this system has two saved runs, and "
                f"it measures portfolio equity against buying {bench} on day one "
                f"and doing nothing.")

    (first_id, first_eq), (last_id, last_eq) = history[0], history[-1]
    port_ret = (last_eq / first_eq - 1.0) if first_eq else 0.0

    bench_ret = run.get("benchmark_return")
    if bench_ret is None:
        return (f"Portfolio is {port_ret:+.2%} from {first_id} to {last_id}. "
                f"No {bench} price series was available this run, so the "
                f"do-nothing comparison could not be computed.")

    diff = port_ret - bench_ret
    verdict = "ahead of" if diff > 0 else ("behind" if diff < 0 else "level with")
    return (f"From {first_id} to {last_id} the portfolio is {port_ret:+.2%}. "
            f"Buying {bench} on {first_id} and doing nothing is {bench_ret:+.2%}. "
            f"The system is {verdict} the do-nothing portfolio by {abs(diff):.2%}. "
            f"Over {len(history)} runs this is far too short a record to mean "
            f"anything, and it is here so the number never goes unmeasured.")


def _equity_history(store: Store) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for run_id in sorted(store.list_runs(limit=400)):
        rec = store.get_run(run_id)
        if rec and rec.get("portfolio", {}).get("equity"):
            out.append((run_id, float(rec["portfolio"]["equity"])))
    return out


def save(run: dict[str, Any], store: Store | None = None) -> str:
    from .config import REPORTS_DIR
    store = store or Store()
    text = render(run, store)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{run['run_id']}.md"
    path.write_text(text, encoding="utf-8")
    return str(path)
