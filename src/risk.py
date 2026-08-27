"""
The deterministic risk gate.

This module decides. The model does not. Every limit is read from
config/risk.yaml and evaluated in plain Python against numbers the pipeline
computed, and a proposal that fails is blocked regardless of how convincing
its thesis reads.

Two tiers of check:
  - symbol level: blocks that one proposal, the rest of the run continues
  - portfolio level: fails the whole proposed allocation

Also enforces the two structural gates from the build guide: a proposal must
carry an exit rule and an invalidation condition, or it is not eligible.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from . import config


@dataclass
class Check:
    rule: str
    passed: bool
    actual: float | str
    limit: float | str
    detail: str
    scope: str = "portfolio"   # "portfolio" | symbol
    severity: str = "hard"     # "hard" blocks; "warn" is reported only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    blocked: dict[str, str] = field(default_factory=dict)
    auto_execute_allowed: bool = False

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "hard"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warn"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "auto_execute_allowed": self.auto_execute_allowed,
            "checks": [c.to_dict() for c in self.checks],
            "blocked": self.blocked,
            "summary": f"{sum(c.passed for c in self.checks)}/{len(self.checks)} passed",
        }


def evaluate(
    proposals: list[dict[str, Any]],
    *,
    account: Any,
    quotes: dict[str, Any] | None = None,
    cfg: dict | None = None,
    broker_mode: str = "paper",
    held_days: dict[str, int | None] | None = None,
) -> Verdict:
    """
    `proposals` are dicts with at least: symbol, asset_class, current_weight,
    target_weight, action, exit_rule, invalidation.

    `held_days` maps symbol to days held, for the minimum holding period. A
    proposal may set `override_holding_period` when a thesis is genuinely
    invalidated or a hard risk limit is breached, which is the difference
    between an investment decision and a reaction to a bad week.
    """
    cfg = cfg or config.risk()
    limits = cfg["limits"]
    gates = cfg.get("gates", {})
    checks: list[Check] = []
    blocked: dict[str, str] = {}

    def add(rule, passed, actual, limit, detail, scope="portfolio", severity="hard"):
        checks.append(Check(rule, passed, actual, limit, detail, scope, severity))
        return passed

    # ---------------- symbol-level ----------------

    max_single = float(limits["max_single_position"])
    min_pos = float(limits.get("min_position", 0.0))

    for p in proposals:
        sym = p["symbol"]
        target = float(p.get("target_weight", 0.0))

        if not add("max_single_position", target <= max_single + 1e-9,
                   round(target, 4), max_single,
                   f"{sym} target {target:.1%} vs cap {max_single:.0%}",
                   scope=sym):
            blocked[sym] = f"target weight {target:.1%} exceeds max_single_position {max_single:.0%}"

        if 0 < target < min_pos - 1e-9:
            add("min_position", False, round(target, 4), min_pos,
                f"{sym} target {target:.1%} below floor {min_pos:.0%}", scope=sym)
            blocked.setdefault(sym, f"target weight {target:.1%} below min_position {min_pos:.0%}")

        if gates.get("require_exit_rule") and p.get("action") not in ("KEEP", "EXIT"):
            ok = bool(str(p.get("exit_rule", "")).strip())
            if not add("require_exit_rule", ok, "present" if ok else "missing", "required",
                       f"{sym} must state what makes it exit", scope=sym):
                blocked.setdefault(sym, "no exit rule stated")

        if gates.get("require_thesis_invalidation") and p.get("action") not in ("KEEP", "EXIT"):
            ok = bool(str(p.get("invalidation", "")).strip())
            if not add("require_thesis_invalidation", ok, "present" if ok else "missing",
                       "required", f"{sym} must state what breaks the thesis", scope=sym):
                blocked.setdefault(sym, "no thesis invalidation stated")

        # Buy and hold: a position may not be trimmed or closed by ordinary
        # rebalancing inside the minimum holding window. Only a broken thesis
        # or a hard risk breach gets through, and the proposal has to say so.
        min_hold = int(limits.get("min_holding_days", 0))
        if min_hold and p.get("action") in ("REDUCE", "EXIT"):
            days = (held_days or {}).get(sym)
            override = bool(p.get("override_holding_period"))
            ok = days is None or days >= min_hold or override
            detail = (
                f"{sym} held {days} days of {min_hold} minimum"
                + (", overridden by thesis invalidation" if override else "")
                if days is not None else
                f"{sym} has no recorded open date, holding period not enforced"
            )
            if not add("min_holding_days", ok, days if days is not None else "unknown",
                       min_hold, detail, scope=sym):
                blocked[sym] = (
                    f"held {days} days, inside the {min_hold} day minimum, "
                    f"and nothing marked the thesis as invalidated"
                )

        if gates.get("reject_on_stale_data") and quotes:
            q = quotes.get(sym)
            max_age = float(gates.get("max_data_staleness_hours", 36))
            age = getattr(q, "age_hours", 0.0) if q else 999.0
            if not add("data_freshness", age <= max_age, round(age, 1), max_age,
                       f"{sym} last price is {age:.1f}h old", scope=sym):
                blocked.setdefault(sym, f"price data {age:.1f}h stale (limit {max_age:.0f}h)")

    # Portfolio aggregates are computed from proposals that survived the
    # symbol gate - a blocked name keeps its current weight, it does not
    # silently get the target the model wanted.
    surviving = [p for p in proposals if p["symbol"] not in blocked]
    effective = {
        p["symbol"]: float(p["target_weight"]) for p in surviving
    } | {
        p["symbol"]: float(p.get("current_weight", 0.0)) for p in proposals if p["symbol"] in blocked
    }

    # ---------------- portfolio-level ----------------

    gross = sum(effective.values())
    max_gross = float(limits["max_gross_exposure"])
    add("max_gross_exposure", gross <= max_gross + 1e-9, round(gross, 4), max_gross,
        f"gross exposure {gross:.1%} vs cap {max_gross:.0%}")

    cash_weight = 1.0 - gross
    min_cash = float(limits["min_cash"])
    add("min_cash", cash_weight >= min_cash - 1e-9, round(cash_weight, 4), min_cash,
        f"cash {cash_weight:.1%} vs floor {min_cash:.0%}")

    max_class = float(limits.get("max_class_exposure", 1.0))
    by_class: dict[str, float] = {}
    for p in proposals:
        w = effective.get(p["symbol"], 0.0)
        by_class[p.get("asset_class", "unknown")] = by_class.get(p.get("asset_class", "unknown"), 0.0) + w
    for cls, w in sorted(by_class.items()):
        add("max_class_exposure", w <= max_class + 1e-9, round(w, 4), max_class,
            f"{cls} exposure {w:.1%} vs cap {max_class:.0%}")

    opens = [p for p in surviving
             if float(p.get("current_weight", 0.0)) == 0.0 and float(p["target_weight"]) > 0.0]
    max_new = int(limits.get("max_new_positions_per_run", 999))
    add("max_new_positions_per_run", len(opens) <= max_new, len(opens), max_new,
        f"{len(opens)} new positions proposed this run")

    turnover = sum(
        abs(effective.get(p["symbol"], 0.0) - float(p.get("current_weight", 0.0)))
        for p in proposals
    )
    max_turn = float(limits.get("max_turnover_per_run", 1.0))
    add("max_turnover_per_run", turnover <= max_turn + 1e-9, round(turnover, 4), max_turn,
        f"total weight change {turnover:.1%} vs cap {max_turn:.0%}")

    if account is not None:
        equity = float(getattr(account, "equity", 0.0))
        add("account_funded", equity > 0, round(equity, 2), "> 0",
            f"account equity ${equity:,.2f}")

    # ---------------- authority ----------------
    #
    # Two independent conditions must both hold before code is ever allowed to
    # place an order without a human click. Config alone cannot unlock a live
    # account: the mode check is not readable from risk.yaml on purpose.
    requires_approval = bool(gates.get("require_human_approval", True))
    is_paper = broker_mode == "paper"
    add("require_human_approval", True, "enforced" if requires_approval else "waived",
        "policy", "every order needs a dashboard approval" if requires_approval
        else "auto-execution permitted by config", severity="warn")
    if not is_paper:
        add("live_account_guard", True, "live", "paper",
            "broker is in LIVE mode - auto-execution is refused unconditionally",
            severity="warn")

    # Only portfolio-scope failures void the run. A symbol-scope failure blocks
    # that one name and is recorded in `blocked`; the rest of the run stands.
    # Conflating the two meant one position inside its holding window blocked
    # every unrelated proposal in the same run.
    verdict_passed = all(
        c.passed for c in checks
        if c.severity == "hard" and c.scope == "portfolio"
    )
    return Verdict(
        passed=verdict_passed,
        checks=checks,
        blocked=blocked,
        auto_execute_allowed=(not requires_approval) and is_paper and verdict_passed,
    )


def approvable(proposal: dict[str, Any], verdict: Verdict) -> tuple[bool, str]:
    """Can this single proposal be sent to the broker once a human approves?"""
    sym = proposal["symbol"]
    if sym in verdict.blocked:
        return False, verdict.blocked[sym]
    if not verdict.passed:
        failed = ", ".join(c.rule for c in verdict.failures if c.scope == "portfolio")
        if failed:
            return False, f"portfolio-level risk failure: {failed}"
    return True, ""
