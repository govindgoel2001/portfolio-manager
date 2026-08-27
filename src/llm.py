"""
The reasoning layer, served by Hermes.

Every language model call the hedge fund makes goes through here: the analyst
pass that writes each thesis, the executive summary, the residual risk note,
the news event classifier, and sentiment. There is one backend and one place
to change it.

The default backend is Hermes, the agent already running on this box, reached
over the headless opencode HTTP API. That means the whole system runs on the
opencode subscription that is already paid for, with no second API key, and
every call is attributable to the same agent that manages the machine.

The Anthropic path is still here as a fallback for running off the VPS, and it
is only used when explicitly selected or when Hermes is unreachable and a key
happens to exist.

What has not changed, and must not: the model never produces a number that
reaches a decision. It receives features and scores this system computed and
writes prose about them. If the arithmetic and the prose disagree, the
arithmetic is what the risk gate reads.

With no backend at all, every caller falls back to deterministic text derived
from the rubric, and the pipeline still runs end to end.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx

from .config import PROMPT_DIR

log = logging.getLogger(__name__)

BACKEND = os.environ.get("PM_LLM_BACKEND", "hermes").lower()

# Hermes, over the opencode headless server.
HERMES_URL = os.environ.get("PM_HERMES_URL", "http://172.19.0.1:4096").rstrip("/")
HERMES_MODEL = os.environ.get("PM_HERMES_MODEL", "opencode-go/kimi-k3")
HERMES_USER = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
HERMES_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
HERMES_TIMEOUT = float(os.environ.get("PM_HERMES_TIMEOUT", "300"))

# Anthropic, only as a fallback.
ANTHROPIC_MODEL = os.environ.get("PM_MODEL", "claude-sonnet-5")
MAX_TOKENS = 8000

_health: tuple[float, bool] | None = None
HEALTH_TTL = 60.0


class LLMUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------

def hermes_reachable(force: bool = False) -> bool:
    """Cached, because this is called on every degradation decision."""
    global _health
    now = time.monotonic()
    if _health and not force and now - _health[0] < HEALTH_TTL:
        return _health[1]
    ok = False
    try:
        r = httpx.get(f"{HERMES_URL}/config", auth=_auth(), timeout=8.0)
        ok = r.status_code == 200
    except httpx.HTTPError as e:
        log.debug("hermes not reachable at %s: %s", HERMES_URL, e)
    _health = (now, ok)
    return ok


def anthropic_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def available() -> bool:
    """Is any reasoning backend usable right now."""
    if BACKEND == "none":
        return False
    if BACKEND == "anthropic":
        return anthropic_available()
    return hermes_reachable() or anthropic_available()


def backend_name() -> str:
    """What actually answered, for the memo's audit line."""
    if BACKEND == "none":
        return "disabled"
    if BACKEND == "anthropic":
        return "anthropic" if anthropic_available() else "unavailable"
    if hermes_reachable():
        return f"hermes ({HERMES_MODEL})"
    if anthropic_available():
        return f"anthropic ({ANTHROPIC_MODEL}), hermes unreachable"
    return "unavailable"


def _auth() -> tuple[str, str] | None:
    return (HERMES_USER, HERMES_PASSWORD) if HERMES_PASSWORD else None


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing prompt: {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the one call everything funnels through
# --------------------------------------------------------------------------

def complete(system: str, user: str, *, model: str | None = None,
             max_tokens: int = MAX_TOKENS) -> str:
    """
    Public entry point for callers that supply their own prompt, such as the
    committee, which needs to address a specific model rather than the default.
    Raises LLMUnavailable rather than returning a stand-in, because a caller
    running several models against each other has to be able to tell the
    difference between a seat that abstained and a seat that answered.
    """
    return _complete(system, user, model=model, max_tokens=max_tokens)


def _complete(system: str, user: str, *, model: str | None = None,
              max_tokens: int = MAX_TOKENS) -> str:
    if BACKEND == "none":
        raise LLMUnavailable("PM_LLM_BACKEND is none")

    if BACKEND != "anthropic" and hermes_reachable():
        try:
            return _complete_via_hermes(system, user, model=model)
        except LLMUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("hermes call failed (%s), trying anthropic", e)
            if not anthropic_available():
                raise LLMUnavailable(f"hermes failed and no Anthropic key: {e}") from e

    if anthropic_available():
        return _complete_via_anthropic(system, user, model=model,
                                       max_tokens=max_tokens)

    raise LLMUnavailable(
        f"no reasoning backend: hermes unreachable at {HERMES_URL} and "
        f"ANTHROPIC_API_KEY is not set")


def _complete_via_hermes(system: str, user: str, *, model: str | None = None) -> str:
    """
    One throwaway session per call.

    Deliberately not reused: the analyst pass must not see the sentiment
    prompt's history, and a long-lived session would quietly turn independent
    scoring calls into a conversation that drifts.
    """
    provider, _, model_id = (model or HERMES_MODEL).partition("/")
    if not model_id:
        provider, model_id = "opencode-go", provider

    session_id = None
    try:
        with httpx.Client(timeout=HERMES_TIMEOUT, auth=_auth()) as client:
            r = client.post(f"{HERMES_URL}/session",
                            json={"title": "portfolio-manager"})
            if r.status_code == 401:
                raise LLMUnavailable("hermes rejected the server password")
            r.raise_for_status()
            session_id = r.json().get("id")
            if not session_id:
                raise RuntimeError(f"no session id in response: {r.text[:200]}")

            body = {
                "model": {"providerID": provider, "modelID": model_id},
                "system": system,
                # These are reasoning calls over data already in the prompt.
                # Tools would only give the model a way to wander off and
                # read the filesystem instead of answering.
                "tools": {t: False for t in
                          ("bash", "edit", "write", "read", "glob", "grep",
                           "list", "webfetch", "websearch", "task")},
                "parts": [{"type": "text", "text": user}],
            }
            r = client.post(f"{HERMES_URL}/session/{session_id}/message", json=body)
            r.raise_for_status()
            return _text_of(r.json())
    finally:
        if session_id:
            # Sessions accumulate on disk otherwise, one per scored symbol.
            try:
                httpx.delete(f"{HERMES_URL}/session/{session_id}",
                             auth=_auth(), timeout=10.0)
            except httpx.HTTPError:
                pass


def _text_of(payload: dict[str, Any]) -> str:
    parts = payload.get("parts") or []
    chunks = [p.get("text", "") for p in parts
              if isinstance(p, dict) and p.get("type") == "text"]
    text = "\n".join(c for c in chunks if c).strip()
    if not text:
        raise RuntimeError(f"no text in hermes reply: {json.dumps(payload)[:300]}")
    return text


def _complete_via_anthropic(system: str, user: str, *, model: str | None = None,
                            max_tokens: int = MAX_TOKENS) -> str:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("anthropic package is not installed") from e
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model or ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(getattr(block, "text", "") for block in msg.content)


def _extract_json(text: str) -> Any:
    """Models like to wrap JSON in prose or a fence. Dig it out, don't guess."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start == -1 or end <= start:
        start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        return json.loads(candidate[start:end + 1])
    raise ValueError("no JSON object found in model response")


# --------------------------------------------------------------------------
# analyst pass
# --------------------------------------------------------------------------

def analyst_pass(
    proposals: list[dict[str, Any]],
    snapshot: dict[str, Any],
    prior_theses: dict[str, Any],
    *,
    model: str | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    """
    Returns (by_symbol_annotations, source). Annotations carry thesis, counter,
    exit_rule, invalidation, confidence and role.
    """
    actionable = [p for p in proposals if p["action"] != "KEEP"] or proposals[:8]
    if not actionable:
        return {}, "deterministic"

    if not available():
        return {p["symbol"]: _deterministic_annotation(p) for p in actionable}, \
               "deterministic"

    payload = {
        "as_of": snapshot.get("snapshot_id"),
        "portfolio_equity": snapshot.get("portfolio", {}).get("equity"),
        "cash_weight": snapshot.get("portfolio", {}).get("cash_weight"),
        "missing_data": snapshot.get("missing_data", []),
        "candidates": [
            {
                "symbol": p["symbol"],
                "asset_class": p["asset_class"],
                "action": p["action"],
                "current_weight": p["current_weight"],
                "target_weight": p["target_weight"],
                "score": p["score"],
                "score_components": p["score_components"],
                "unscored_components": p["unscored"],
                "prior_thesis": prior_theses.get(p["symbol"], {}).get("thesis", ""),
            }
            for p in actionable
        ],
    }

    try:
        text = _complete(load_prompt("analyst"),
                         json.dumps(payload, indent=2, default=str), model=model)
        parsed = _extract_json(text)
        rows = parsed if isinstance(parsed, list) else parsed.get("assets", [])
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            out[sym] = {
                "thesis": str(row.get("thesis", "")).strip(),
                "counter": str(row.get("counter_argument", row.get("counter", ""))).strip(),
                "exit_rule": str(row.get("exit_rule", "")).strip(),
                "invalidation": str(row.get("invalidation", "")).strip(),
                "confidence": str(row.get("confidence", "unrated")).strip().lower(),
                "role": str(row.get("role", "")).strip(),
            }
        for p in actionable:
            if p["symbol"] not in out or not out[p["symbol"]]["exit_rule"]:
                out[p["symbol"]] = _deterministic_annotation(p)
        return out, backend_name()
    except Exception as e:  # noqa: BLE001 - the run must survive a reasoning failure
        log.warning("analyst pass fell back to deterministic text: %s", e)
        return {p["symbol"]: _deterministic_annotation(p) for p in actionable}, \
               "deterministic"


def _deterministic_annotation(p: dict[str, Any]) -> dict[str, str]:
    """
    Honest machine-written stand-ins. These are real, checkable rules derived
    from the rubric, not filler pretending to be analysis.
    """
    comps = p.get("score_components", {})
    strongest = max(comps.items(), key=lambda kv: kv[1], default=("n/a", 0.0))
    weakest = min(comps.items(), key=lambda kv: kv[1], default=("n/a", 0.0))
    unscored = ", ".join(p.get("unscored", [])) or "none"
    return {
        "thesis": (
            f"Rubric score {p['score']:.1f}/100, ranked on {strongest[0]} "
            f"({strongest[1]:.0f}). Action {p['action']} moves the weight from "
            f"{p['current_weight']:.1%} to {p['target_weight']:.1%}."
        ),
        "counter": (
            f"Weakest component is {weakest[0]} at {weakest[1]:.0f}. "
            f"Unscored inputs this run: {unscored}."
        ),
        "exit_rule": (
            "Exit when the total score falls below the configured threshold "
            "for two consecutive runs, or the position breaches "
            "max_single_position."
        ),
        "invalidation": (
            f"Thesis is invalidated if {strongest[0]} drops below the universe "
            f"median while price closes under the slow moving average."
        ),
        "confidence": "low" if p.get("unscored") else "medium",
        "role": f"{p['asset_class']} sleeve",
        "source": "deterministic",
    }


# --------------------------------------------------------------------------
# summary pass
# --------------------------------------------------------------------------

def write_summary(run: dict[str, Any], *, model: str | None = None) -> tuple[str, str]:
    if not available():
        return _deterministic_summary(run), "deterministic"
    try:
        slim = {
            "run_id": run["run_id"],
            "equity": run["portfolio"]["equity"],
            "cash_weight": run["portfolio"]["cash_weight"],
            "benchmark": run.get("benchmark"),
            "risk": {
                "passed": run["risk"]["passed"],
                "failures": [c for c in run["risk"]["checks"] if not c["passed"]],
                "blocked": run["risk"]["blocked"],
            },
            "proposals": [
                {k: p[k] for k in ("symbol", "action", "current_weight",
                                   "target_weight", "score", "thesis")}
                for p in run["proposals"] if p["action"] != "KEEP"
            ],
            "data_coverage": run.get("coverage"),
            "missing_data": run.get("missing_data", []),
        }
        text = _complete(load_prompt("daily_report"),
                         json.dumps(slim, indent=2, default=str),
                         model=model, max_tokens=2000)
        return text.strip(), backend_name()
    except Exception as e:  # noqa: BLE001
        log.warning("summary fell back to deterministic text: %s", e)
        return _deterministic_summary(run), "deterministic"


def _deterministic_summary(run: dict[str, Any]) -> str:
    changes = [p for p in run["proposals"] if p["action"] != "KEEP"]
    risk = run["risk"]
    lines = [
        f"Equity ${run['portfolio']['equity']:,.2f}, "
        f"cash {run['portfolio']['cash_weight']:.1%}, "
        f"{len(run['portfolio']['positions'])} positions.",
    ]
    if not changes:
        lines.append("No proposed changes. Every holding sits inside the rebalance "
                     "band and no watchlist name crossed the score threshold.")
    else:
        by_action: dict[str, list[str]] = {}
        for p in changes:
            by_action.setdefault(p["action"], []).append(p["symbol"])
        lines.append("Proposed: " + "; ".join(
            f"{action} {', '.join(syms)}" for action, syms in sorted(by_action.items())
        ) + ".")
    failed = [c for c in risk["checks"] if not c["passed"]]
    lines.append(
        f"Risk gate {'PASSED' if risk['passed'] else 'FAILED'} ({risk['summary']})."
        + (f" Failed: {', '.join(sorted({c['rule'] for c in failed}))}." if failed else "")
    )
    if run.get("missing_data"):
        lines.append(f"No data for: {', '.join(run['missing_data'])}.")
    return " ".join(lines)
