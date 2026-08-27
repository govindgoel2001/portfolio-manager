"""
Four models, one question, two rounds.

Round one, every seat gets the same evidence and answers independently. They do
not see each other. That independence is the whole point: the moment one model
reads another's answer, agreement stops being evidence of anything.

Round two, cross examination. Each seat's factual claims go back to the other
seats to be marked against the evidence. A model that invented a margin figure
in round one usually gets caught here, because the other three are reading the
same numbers and none of them can find it.

What comes out is a stance, a conviction, and a disagreement index. What does
not come out is an order. The committee can scale a weight the deterministic
allocator already chose, inside a band set in config, and only when enough
seats answered and they were not evenly split. Then the risk gate runs anyway.

A committee that could size a position would eventually talk itself into one.
"""
from __future__ import annotations

import json
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any

from . import config, llm

log = logging.getLogger(__name__)

STANCES = ("BUY", "HOLD", "AVOID")
_STANCE_VALUE = {"BUY": 1.0, "HOLD": 0.0, "AVOID": -1.0}


@dataclass
class Claim:
    """One factual assertion, and what the other seats made of it."""
    text: str
    by: str
    verdicts: dict[str, str] = field(default_factory=dict)  # seat -> supported|contradicted|unverifiable

    @property
    def supported(self) -> int:
        return sum(1 for v in self.verdicts.values() if v == "supported")

    @property
    def contradicted(self) -> int:
        return sum(1 for v in self.verdicts.values() if v == "contradicted")

    @property
    def disputed(self) -> bool:
        return self.contradicted > 0


@dataclass
class Seat:
    name: str
    model: str
    colour: str = "#8b7cf6"
    answered: bool = False
    stance: str = "ABSTAIN"
    conviction: float = 0.0      # 0-10, the seat's own confidence
    bull: list[str] = field(default_factory=list)
    bear: list[str] = field(default_factory=list)
    key_risk: str = ""
    claims: list[str] = field(default_factory=list)
    error: str = ""
    latency_s: float = 0.0


@dataclass
class Verdict:
    symbol: str
    seats: list[Seat]
    consensus: str               # BUY | HOLD | AVOID | NO CONSENSUS
    disagreement: float          # 0 unanimous, 100 maximally split
    score: float                 # 0-10, conviction signed by stance
    answered: int
    total: int
    claims: list[Claim] = field(default_factory=list)
    multiplier: float = 1.0      # what the allocator is allowed to use
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["claims"] = [
            asdict(c) | {"supported": c.supported,
                         "contradicted": c.contradicted,
                         "disputed": c.disputed}
            for c in self.claims
        ]
        return d


# --------------------------------------------------------------------------

def _cfg() -> dict[str, Any]:
    return config.load("committee")


def seats() -> list[Seat]:
    return [Seat(name=s["name"], model=s["model"], colour=s.get("colour", "#8b7cf6"))
            for s in _cfg().get("seats", [])]


_SYSTEM = """You are one seat on an investment committee for a buy and hold \
portfolio. Three other models are answering the same question independently \
and you will not see their answers.

Judge the asset on the evidence given. You have no tools and no browser, so \
every number you cite must appear in the evidence below. If something you would \
need is missing, say it is missing. "Insufficient data" is a real answer here \
and it is better than a confident guess.

The horizon is months to years, not days. A stock that will be volatile next \
week but is a good business at a fair price is a BUY. A stock that will pop on \
Monday is not.

Reply with only a JSON object, no prose around it:
{
  "stance": "BUY" | "HOLD" | "AVOID",
  "conviction": 0-10,
  "bull": ["specific point grounded in the evidence", ...],
  "bear": ["specific point grounded in the evidence", ...],
  "key_risk": "the single thing that would make you wrong",
  "claims": ["a checkable factual statement you are relying on", ...]
}

`claims` are the assertions the other seats will be asked to verify against \
this same evidence. Put the load bearing facts there, not opinions. A claim \
like "revenue grew" is useless; "revenue grew 14% year on year per the \
fundamentals block" can be checked."""


_EXAMINE_SYSTEM = """You are auditing claims another model made about an asset, \
against the evidence below.

For each claim, decide:
  supported     the evidence below states this, or it follows directly
  contradicted  the evidence below says otherwise
  unverifiable  the evidence does not settle it either way

You are not judging whether the claim is a good argument. Only whether the \
evidence supports it. A claim that is probably true in the real world but is \
not in this evidence is `unverifiable`, not `supported`.

Reply with only a JSON array:
[{"claim_index": 0, "verdict": "supported", "why": "one short sentence"}, ...]"""


def _evidence_block(symbol: str, evidence: dict[str, Any]) -> str:
    return json.dumps({"symbol": symbol} | evidence, indent=2, default=str)


def _ask_seat(seat: Seat, symbol: str, evidence: str) -> Seat:
    import time
    started = time.time()
    try:
        text = llm.complete(_SYSTEM, evidence, model=seat.model)
        parsed = llm._extract_json(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"expected an object, got {type(parsed).__name__}")

        stance = str(parsed.get("stance", "")).strip().upper()
        if stance not in STANCES:
            raise ValueError(f"unusable stance {stance!r}")

        seat.stance = stance
        seat.conviction = max(0.0, min(10.0, float(parsed.get("conviction", 5))))
        seat.bull = [str(x).strip() for x in (parsed.get("bull") or [])][:6]
        seat.bear = [str(x).strip() for x in (parsed.get("bear") or [])][:6]
        seat.key_risk = str(parsed.get("key_risk", "")).strip()
        seat.claims = [str(x).strip() for x in (parsed.get("claims") or [])
                       ][:int(_cfg().get("max_claims_per_seat", 5))]
        seat.answered = True
    except Exception as e:  # noqa: BLE001 - one dead seat is not a dead committee
        seat.error = f"{type(e).__name__}: {e}"[:200]
        log.warning("seat %s (%s) did not answer: %s", seat.name, seat.model,
                    seat.error)
    seat.latency_s = round(time.time() - started, 2)
    return seat


def _cross_examine(panel: list[Seat], evidence: str) -> list[Claim]:
    """
    Every answering seat audits every other seat's claims. A seat is never
    asked to mark its own claim, which would only ever return `supported`.
    """
    claims: list[Claim] = []
    for seat in panel:
        for text in seat.claims:
            claims.append(Claim(text=text, by=seat.name))
    if not claims:
        return []

    def audit(auditor: Seat) -> tuple[str, Any]:
        mine = [i for i, c in enumerate(claims) if c.by != auditor.name]
        if not mine:
            return auditor.name, []
        listing = "\n".join(f"{i}. {claims[i].text}" for i in mine)
        prompt = f"{evidence}\n\nCLAIMS TO AUDIT\n{listing}"
        try:
            return auditor.name, llm._extract_json(
                llm.complete(_EXAMINE_SYSTEM, prompt, model=auditor.model))
        except Exception as e:  # noqa: BLE001
            log.warning("seat %s could not audit: %s", auditor.name, e)
            return auditor.name, []

    answering = [s for s in panel if s.answered]
    with ThreadPoolExecutor(max_workers=max(len(answering), 1)) as pool:
        for future in as_completed([pool.submit(audit, s) for s in answering]):
            name, rows = future.result()
            for row in rows if isinstance(rows, list) else []:
                try:
                    idx = int(row.get("claim_index", -1))
                    verdict = str(row.get("verdict", "")).strip().lower()
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0 <= idx < len(claims) and verdict in (
                        "supported", "contradicted", "unverifiable"):
                    claims[idx].verdicts[name] = verdict
    return claims


def convene(symbol: str, evidence: dict[str, Any]) -> Verdict:
    """
    Run the committee on one symbol. Seats are queried in parallel because they
    are independent by design, so there is nothing to serialise.
    """
    cfg = _cfg()
    panel = seats()
    if not cfg.get("enabled", True) or not panel:
        return Verdict(symbol=symbol, seats=[], consensus="NO CONSENSUS",
                       disagreement=0.0, score=0.0, answered=0, total=0,
                       note="committee disabled in config")

    block = _evidence_block(symbol, evidence)

    with ThreadPoolExecutor(max_workers=len(panel)) as pool:
        futures = [pool.submit(_ask_seat, s, symbol, block) for s in panel]
        panel = [f.result() for f in futures]

    # A seat that failed is replaced from the bench rather than silently
    # shrinking the committee, so a flaky provider does not quietly turn a
    # four model verdict into a two model one.
    bench = [Seat(name=r["name"], model=r["model"], colour=r.get("colour", "#8b7cf6"))
             for r in cfg.get("reserves", [])]
    for i, seat in enumerate(panel):
        if seat.answered or not bench:
            continue
        stand_in = _ask_seat(bench.pop(0), symbol, block)
        if stand_in.answered:
            stand_in.name = f"{stand_in.name} (for {seat.name})"
            panel[i] = stand_in

    claims: list[Claim] = []
    if cfg.get("cross_examine", True):
        claims = _cross_examine(panel, block)

    return _tally(symbol, panel, claims, cfg)


def _tally(symbol: str, panel: list[Seat], claims: list[Claim],
           cfg: dict[str, Any]) -> Verdict:
    answering = [s for s in panel if s.answered]
    total, answered = len(panel), len(answering)

    if not answering:
        return Verdict(symbol=symbol, seats=panel, consensus="NO CONSENSUS",
                       disagreement=0.0, score=0.0, answered=0, total=total,
                       claims=claims, multiplier=1.0,
                       note="no seat answered, so nothing here is a committee view")

    values = [_STANCE_VALUE[s.stance] for s in answering]
    mean = statistics.fmean(values)

    # Spread across {-1, 0, 1}. An even split between BUY and AVOID gives a
    # population standard deviation of 1.0, which is the worst case, so the
    # index is that deviation expressed as a percentage of it.
    spread = statistics.pstdev(values) if len(values) > 1 else 0.0
    disagreement = round(min(spread, 1.0) * 100, 1)

    minimum = int(cfg.get("min_seats_for_consensus", 3))
    if answered < minimum:
        consensus = "NO CONSENSUS"
        note = (f"only {answered} of {total} seats answered, below the "
                f"{minimum} this config requires before calling it a consensus")
    else:
        consensus = ("BUY" if mean >= 0.5 else
                     "AVOID" if mean <= -0.5 else "HOLD")
        note = f"{answered} of {total} seats answered"

    # Conviction is what the seats that agree with the consensus actually felt,
    # not an average across seats that disagreed. Averaging a 9 conviction BUY
    # with a 9 conviction AVOID would produce a confident looking 9 for a
    # committee that is in fact deadlocked.
    aligned = [s for s in answering if s.stance == consensus] or answering
    score = round(statistics.fmean([s.conviction for s in aligned]), 1)

    disputed = [c for c in claims if c.disputed]
    if disputed:
        note += f", {len(disputed)} of {len(claims)} claims disputed on cross examination"

    return Verdict(
        symbol=symbol, seats=panel, consensus=consensus,
        disagreement=disagreement, score=score,
        answered=answered, total=total, claims=claims,
        multiplier=_multiplier(consensus, score, disagreement, answered, cfg),
        note=note,
    )


def _multiplier(consensus: str, score: float, disagreement: float,
                answered: int, cfg: dict[str, Any]) -> float:
    """
    The only number the committee is allowed to export.

    It scales a weight the allocator already decided on. It cannot create a
    position, remove one, or widen a limit, and the risk gate runs on the
    result regardless of what it says.
    """
    rules = cfg.get("influence", {})
    lo = float(rules.get("min_multiplier", 0.8))
    hi = float(rules.get("max_multiplier", 1.2))

    if answered < int(rules.get("require_seats", 3)):
        return 1.0
    if disagreement > float(rules.get("max_disagreement", 60)):
        return 1.0
    if consensus == "BUY":
        return round(1.0 + (hi - 1.0) * (score / 10.0), 4)
    if consensus == "AVOID":
        return round(1.0 - (1.0 - lo) * (score / 10.0), 4)
    return 1.0
