"""
Manual trade entry.

You type a symbol and a size, and it lands in the same approval queue the
automated pipeline writes to, having passed the same risk gate. It is not a
side door. There is exactly one path from an intention to a broker order, and
this joins it near the start rather than skipping to the end.

That is a deliberate constraint on the person using it, not just on the model.
The limits in risk.yaml were set by someone thinking calmly about position
sizing. The moment there is a manual route that skips them, they stop being
limits and become suggestions that apply only when nobody feels strongly.

The one thing a human may override that the model may not is the minimum
holding period, because deciding a thesis is dead is a judgement call and a
calendar cannot make it. The override is recorded on the proposal with the
stated reason.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, asdict
from typing import Any

from . import config, risk, state

log = logging.getLogger(__name__)

SIDES = ("BUY", "SELL")


@dataclass
class Request:
    symbol: str
    side: str
    #: exactly one of these. Weight is a share of equity, notional is dollars.
    target_weight: float | None = None
    notional: float | None = None
    reason: str = ""
    exit_rule: str = ""
    invalidation: str = ""
    override_holding_period: bool = False


@dataclass
class Decision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reject(*reasons: str) -> Decision:
    return Decision(accepted=False, reasons=list(reasons))


def submit(req: Request, *, account: Any, positions: dict[str, Any],
           quotes: dict[str, Any] | None = None,
           store: state.Store | None = None,
           held_days: dict[str, int | None] | None = None) -> Decision:
    """
    Validate, gate, and queue. Returns a Decision either way; the caller shows
    the reasons rather than a generic failure, because "rejected" with no
    explanation is how people learn to route around a control.
    """
    store = store or state.Store()
    symbol = (req.symbol or "").upper().strip()
    side = (req.side or "").upper().strip()

    # ---- shape ----------------------------------------------------------
    if side not in SIDES:
        return _reject(f"side must be BUY or SELL, got {req.side!r}")

    universe = {s.upper() for s in config.all_symbols()}
    if symbol not in universe:
        # The universe is the hard boundary on what this system may ever hold,
        # and a manual order is not a reason to widen it. Editing
        # config/universe.yaml is, because that is a decision made once and
        # reviewed, rather than at the moment of wanting to buy something.
        return _reject(
            f"{symbol} is not in the universe. Add it to config/universe.yaml "
            f"and redeploy if it belongs there.")

    if (req.target_weight is None) == (req.notional is None):
        return _reject("give exactly one of target_weight or notional")

    equity = float(getattr(account, "equity", 0.0) or 0.0)
    if equity <= 0:
        return _reject("account equity is zero or unknown, so no weight can be computed")

    # ---- size -----------------------------------------------------------
    if req.notional is not None:
        if req.notional <= 0:
            return _reject("notional must be positive")
        delta_weight = float(req.notional) / equity
    else:
        delta_weight = None

    held = positions.get(symbol)
    current_weight = float(getattr(held, "weight", 0.0) or 0.0) if held else 0.0

    if req.target_weight is not None:
        if not 0.0 <= req.target_weight <= 1.0:
            return _reject("target_weight must be between 0 and 1")
        target_weight = float(req.target_weight)
        if side == "SELL" and target_weight > current_weight:
            return _reject(
                f"a SELL cannot raise the weight: {symbol} is at "
                f"{current_weight:.2%} and the target is {target_weight:.2%}")
        if side == "BUY" and target_weight < current_weight:
            return _reject(
                f"a BUY cannot lower the weight: {symbol} is at "
                f"{current_weight:.2%} and the target is {target_weight:.2%}")
    else:
        target_weight = (current_weight + delta_weight if side == "BUY"
                         else max(current_weight - delta_weight, 0.0))

    if abs(target_weight - current_weight) < 1e-6:
        return _reject(f"that is the weight {symbol} already has, so there is nothing to do")

    # ---- the same gate the pipeline uses --------------------------------
    proposal = {
        "id": state.proposal_id(_run_id(), symbol),
        "symbol": symbol,
        "asset_class": config.class_of(symbol),
        "action": "BUY" if target_weight > current_weight else "SELL",
        "current_weight": round(current_weight, 6),
        "target_weight": round(target_weight, 6),
        "score": None,
        "score_components": {},
        "unscored": [],
        "source": "manual",
        "reason": req.reason.strip(),
        "exit_rule": req.exit_rule.strip(),
        "invalidation": req.invalidation.strip(),
        "override_holding_period": bool(req.override_holding_period),
        "status": state.PENDING,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    # The rubric cannot speak for a manual order, so the exit rule and the
    # invalidation have to come from the person. They are what the risk gate
    # checks and what a future run compares against; without them there is
    # nothing to hold the decision to later.
    missing = [k for k in ("exit_rule", "invalidation") if not proposal[k]]
    if missing:
        return _reject(
            f"a manual order needs {' and '.join(missing)}: state what would "
            f"make you sell and what would prove the idea wrong")

    verdict = risk.evaluate([proposal], account=account, quotes=quotes,
                            held_days=held_days)
    ok, why = risk.approvable(proposal, verdict)
    checks = [asdict(c) for c in verdict.checks]
    warnings = [c.detail for c in verdict.warnings]

    if not ok:
        return Decision(accepted=False, reasons=[why], warnings=warnings,
                        proposal=proposal, checks=checks)

    _queue(store, proposal)
    log.info("manual %s %s to %.2f%% queued for approval",
             proposal["action"], symbol, target_weight * 100)
    return Decision(accepted=True, reasons=[], warnings=warnings,
                    proposal=proposal, checks=checks)


def _run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-manual")


def _queue(store: state.Store, proposal: dict[str, Any]) -> None:
    """
    Manual orders live in their own dated run so they never overwrite a
    scheduled one, and so the audit trail shows plainly which decisions a
    person made and which the pipeline made.
    """
    run_id = _run_id()
    run = store.get_run(run_id) or {
        "run_id": run_id,
        "kind": "manual",
        "created_at": proposal["created_at"],
        "proposals": [],
        "summary": "Manually entered orders. Each one passed the same risk "
                   "gate as a scheduled proposal and still needs approval.",
    }
    run["proposals"] = [p for p in run["proposals"]
                        if p.get("id") != proposal["id"]] + [proposal]
    store.save_run(run)
