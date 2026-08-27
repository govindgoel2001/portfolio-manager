"""
Dashboard API.

Serves the live paper portfolio, the daily memo, and the approval queue. The
only endpoint that can move money is POST /api/proposals/{id}/approve, and it
re-runs the risk gate before it sends anything, so a proposal cannot be
approved into a state that has since become unsafe.

Auth is a single shared password from PM_DASHBOARD_PASSWORD, exchanged for a
signed cookie. It is deliberately simple, and it is the only thing between the
public internet and an order button, so the app refuses to start without it
unless PM_ALLOW_NO_AUTH=1 is set for local use.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
import time
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import (committee, config, ingest, llm, manual,  # noqa: E402
                 portfolio_risk, rag, risk,
                 stream)
from src import basket as basket_mod  # noqa: E402
from src import connections, reconcile  # noqa: E402
from src import objective, score as score_mod, trackers  # noqa: E402
from src.data import sectors as sectors_mod  # noqa: E402
from src.data import smartmoney as sm  # noqa: E402
from src.brokers.base import BrokerError, OrderRequest  # noqa: E402
from src.brokers.registry import Registry  # noqa: E402
from src.state import APPROVED, EXECUTED, FAILED, PENDING, REJECTED, Store  # noqa: E402

log = logging.getLogger("api")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
COOKIE_NAME = "pm_session"

PASSWORD = os.environ.get("PM_DASHBOARD_PASSWORD", "")
ALLOW_NO_AUTH = os.environ.get("PM_ALLOW_NO_AUTH") == "1"
SECRET = os.environ.get("PM_SECRET_KEY") or secrets.token_hex(32)

if not PASSWORD and not ALLOW_NO_AUTH:
    raise SystemExit(
        "PM_DASHBOARD_PASSWORD is not set. Set it, or set PM_ALLOW_NO_AUTH=1 "
        "to run without auth on localhost only."
    )

# PM_ALLOW_NO_AUTH turns the dashboard into an open one. The docstring said
# "localhost only" and nothing enforced it, so a stray environment variable on
# the deployed instance would have served the whole thing, including the broker
# connection screen, to anyone who found the URL. PM_HOSTNAME is only set for a
# public vhost, so the two together are always a mistake.
if ALLOW_NO_AUTH and os.environ.get("PM_HOSTNAME"):
    raise SystemExit(
        "PM_ALLOW_NO_AUTH=1 is set alongside PM_HOSTNAME "
        f"({os.environ['PM_HOSTNAME']}). That would serve an unauthenticated "
        "dashboard on a public hostname. Unset one of them."
    )

# Bumped on logout so a session cookie actually stops working, rather than
# only being dropped by a browser that chooses to cooperate. It also means a
# restart invalidates outstanding cookies, which is the behaviour you want
# after rotating anything.
_SESSION_EPOCH = [secrets.token_hex(8)]

# Login attempts per address. The password is long and random so this is not
# the last line of defence, but an unthrottled login on a public host is free
# for an attacker and cheap for us to close.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300

app = FastAPI(title="AI portfolio manager", docs_url=None, redoc_url=None)
store = Store()
_registry: Registry | None = None


def registry() -> Registry:
    """Rebuilt lazily so a key added to .env takes effect on the next restart."""
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def _token() -> str:
    return hmac.new(SECRET.encode(),
                    (PASSWORD + _SESSION_EPOCH[0]).encode(),
                    hashlib.sha256).hexdigest()


def _rate_limited(request: Request) -> bool:
    """True when this address has had too many recent failures."""
    now = time.time()
    who = (request.client.host if request.client else "unknown")
    tries = [t for t in _LOGIN_ATTEMPTS.get(who, []) if now - t < LOGIN_WINDOW_SECONDS]
    _LOGIN_ATTEMPTS[who] = tries
    return len(tries) >= LOGIN_MAX_ATTEMPTS


def _record_failure(request: Request) -> None:
    who = (request.client.host if request.client else "unknown")
    _LOGIN_ATTEMPTS.setdefault(who, []).append(time.time())


def require_auth(pm_session: str | None = Cookie(default=None)) -> bool:
    if ALLOW_NO_AUTH:
        return True
    if not pm_session or not hmac.compare_digest(pm_session, _token()):
        raise HTTPException(status_code=401, detail="not authenticated")
    return True


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response, request: Request) -> dict[str, Any]:
    if ALLOW_NO_AUTH:
        return {"ok": True, "auth": "disabled"}
    if _rate_limited(request):
        raise HTTPException(
            status_code=429,
            detail=f"too many attempts, wait {LOGIN_WINDOW_SECONDS // 60} minutes")
    if not hmac.compare_digest(body.password, PASSWORD):
        _record_failure(request)
        raise HTTPException(status_code=401, detail="wrong password")
    response.set_cookie(
        COOKIE_NAME, _token(), httponly=True, samesite="lax",
        secure=os.environ.get("PM_COOKIE_SECURE", "1") == "1",
        max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    # Rotating the epoch invalidates every outstanding cookie server side.
    # Deleting the cookie alone only asks the browser to forget it, which does
    # nothing about a copy someone already has.
    _SESSION_EPOCH[0] = secrets.token_hex(8)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/session")
def session(pm_session: str | None = Cookie(default=None)) -> dict[str, bool]:
    if ALLOW_NO_AUTH:
        return {"authenticated": True, "auth_required": False}
    ok = bool(pm_session and hmac.compare_digest(pm_session, _token()))
    return {"authenticated": ok, "auth_required": True}


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------

@app.get("/api/universe")
def universe(_: bool = Depends(require_auth)) -> dict[str, Any]:
    u = config.universe()
    reg = registry()
    return {
        "benchmark": u.get("benchmark"),
        "classes": [
            {
                "key": key,
                "label": spec.get("label", key),
                "icon": spec.get("icon", "chart"),
                "symbols": [s.upper() for s in spec["symbols"]],
                "proxy_for": spec.get("proxy_for"),
                "broker": reg.for_class(key).name,
                "configured_broker": spec.get("broker"),
            }
            for key, spec in u["classes"].items()
        ],
    }


@app.get("/api/health")
def health(_: bool = Depends(require_auth)) -> dict[str, Any]:
    reg = registry()
    return {
        "degraded": reg.degraded(),
        "routing": {k: reg.for_class(k).name for k in config.universe()["classes"]},
        "failures": reg.failures,
        "brokers": [
            {"name": h.name, "ok": h.ok, "mode": h.mode,
             "detail": h.detail, "supports": list(h.supports)}
            for h in reg.health()
        ],
        "llm": llm.available(),
        "llm_backend": llm.backend_name(),
        "server_time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/api/portfolio")
def portfolio(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Live from the broker on every call. This is what makes the page live."""
    reg = registry()
    accounts, positions, errors = {}, [], []
    for name, broker in reg.active().items():
        try:
            accounts[name] = broker.get_account().to_dict()
            for p in broker.get_positions():
                d = p.to_dict()
                d["universe_class"] = config.class_of(p.symbol)
                positions.append(d)
        except BrokerError as e:
            errors.append(f"{name}: {e}")

    equity = sum(a["equity"] for a in accounts.values())
    cash = sum(a["cash"] for a in accounts.values())
    for p in positions:
        p["weight"] = p["market_value"] / equity if equity else 0.0

    by_class: dict[str, float] = {}
    for p in positions:
        by_class[p["universe_class"]] = by_class.get(p["universe_class"], 0.0) + p["weight"]

    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "cash_weight": round(cash / equity, 6) if equity else 0.0,
        "positions": sorted(positions, key=lambda x: -x["market_value"]),
        "exposure_by_class": {k: round(v, 6) for k, v in by_class.items()},
        "accounts": accounts,
        "errors": errors,
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/api/quotes")
def quotes(symbols: str = "", _: bool = Depends(require_auth)) -> dict[str, Any]:
    reg = registry()
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or config.all_symbols()
    out: dict[str, Any] = {}
    for _name, (broker, syms) in reg.by_broker(wanted).items():
        try:
            for sym, q in broker.get_quotes(syms).items():
                out[sym] = {"price": q.price, "t": q.ts.isoformat(),
                            "stale": q.stale, "age_hours": round(q.age_hours, 2)}
        except BrokerError as e:
            log.warning("quotes failed: %s", e)
    return out


@app.get("/api/history")
def history(symbol: str, days: int = 90, _: bool = Depends(require_auth)) -> dict[str, Any]:
    reg = registry()
    broker = reg.for_symbol(symbol.upper())
    try:
        bars = broker.get_bars([symbol.upper()], days).get(symbol.upper(), [])
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e
    return {
        "symbol": symbol.upper(),
        "closes": [{"t": b.ts.date().isoformat(), "c": b.close} for b in bars[-days:]],
    }


@app.get("/api/equity-curve")
def equity_curve(days: int = 90, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Real daily equity from the broker, plus the benchmark bought on the same
    first day and held.

    Built from the broker's own history rather than from saved runs, because
    run ids are calendar dates: a run-based curve gains one point per day and
    shows nothing at all until the second day.
    """
    reg = registry()
    merged: dict[str, float] = {}
    source = "broker"

    for name, broker in reg.active().items():
        try:
            for point in broker.get_portfolio_history(days):
                d = point.to_dict()
                merged[d["t"]] = merged.get(d["t"], 0.0) + d["equity"]
        except BrokerError as e:
            log.warning("portfolio history failed on %s: %s", name, e)

    if not merged:
        # No adapter supplied history. One point per saved run is all there is.
        source = "runs"
        for run_id in sorted(store.list_runs(limit=400)):
            rec = store.get_run(run_id)
            if rec and rec.get("portfolio", {}).get("equity"):
                merged[run_id] = float(rec["portfolio"]["equity"])

    # Always end on the live number, so the curve agrees with the header.
    try:
        live = sum(b.get_account().equity for b in reg.active().values())
        if live:
            merged[dt.datetime.now(dt.timezone.utc).date().isoformat()] = round(live, 2)
    except BrokerError as e:
        log.warning("live equity unavailable for the curve: %s", e)

    points = [{"t": t, "equity": round(v, 2)} for t, v in sorted(merged.items())]

    # A brand new account has a flat line at the starting balance: true, and
    # useless. When the realised curve has not moved yet, show what the current
    # allocation would have done over the trailing window instead, labelled as
    # a look back rather than as returns anyone earned.
    distinct = len({p["equity"] for p in points})
    if distinct <= 2:
        hypothetical = _allocation_backtest(reg)
        if hypothetical:
            return hypothetical

    return {
        "points": points,
        "benchmark": _benchmark_series(reg, points),
        "benchmark_symbol": config.universe().get("benchmark"),
        "source": source,
        "realised": True,
    }


def _allocation_backtest(reg: Registry, days: int = 260) -> dict[str, Any] | None:
    """Today's weights held across the trailing window, against the benchmark."""
    bench = (config.universe().get("benchmark") or "SPY").upper()
    try:
        equity = sum(b.get_account().equity for b in reg.active().values())
        positions = [p for b in reg.active().values() for p in b.get_positions()]
    except BrokerError:
        return None
    if not positions or not equity:
        return None

    weights = {p.symbol.upper(): p.market_value / equity for p in positions}
    symbols = list(weights) + [bench]
    bars: dict[str, Any] = {}
    for _name, (broker, syms) in reg.by_broker(symbols).items():
        try:
            bars.update(broker.get_bars(syms, days))
        except BrokerError as e:
            log.warning("backtest bars failed: %s", e)

    bt = portfolio_risk.backtest_weights(weights, bars, bench,
                                         starting_equity=equity)
    if bt is None:
        return None

    return {
        "points": [{"t": d, "equity": v} for d, v in zip(bt.days, bt.portfolio)],
        "benchmark": [{"t": d, "equity": v} for d, v in zip(bt.days, bt.benchmark)],
        "benchmark_symbol": bench,
        "source": "allocation_backtest",
        "realised": False,
        "stats": {
            "total_return": bt.total_return,
            "benchmark_return": bt.benchmark_return,
            "excess": bt.excess,
            "max_drawdown": bt.max_drawdown,
            "benchmark_max_drawdown": bt.benchmark_max_drawdown,
            "volatility": bt.volatility,
            "sharpe": bt.sharpe,
            "beat_benchmark": bt.beat_benchmark,
        },
        "note": bt.note,
    }


@app.get("/api/concentration")
def concentration(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """How many independent bets the book really holds."""
    reg = registry()
    try:
        equity = sum(b.get_account().equity for b in reg.active().values())
        positions = [p for b in reg.active().values() for p in b.get_positions()]
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e
    if not positions or not equity:
        return {"nominal_n": 0, "effective_n": 0, "note": "no positions"}

    weights = {p.symbol.upper(): p.market_value / equity for p in positions}
    bars: dict[str, Any] = {}
    for _name, (broker, syms) in reg.by_broker(list(weights)).items():
        try:
            bars.update(broker.get_bars(syms, 260))
        except BrokerError as e:
            log.warning("concentration bars failed: %s", e)
    return portfolio_risk.analyse(weights, bars).to_dict()


@app.get("/api/stream")
def stream_state(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Whether real-time news is actually connected, not whether it should be."""
    return stream.read_status()


@app.get("/api/reasoning")
def reasoning_state(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Which reasoning backend is answering, so the dashboard can stop guessing."""
    return {
        "backend": llm.BACKEND,
        "resolved": llm.backend_name(),
        "hermes_url": llm.HERMES_URL,
        "hermes_model": llm.HERMES_MODEL,
        "hermes_reachable": llm.hermes_reachable(),
        "anthropic_key_present": llm.anthropic_available(),
        "available": llm.available(),
    }


def _score_universe() -> dict[str, Any]:
    """Score every symbol from live bars. Shared by the basket endpoints."""
    from src.data import fundamentals as fu
    reg = registry()
    syms = list(config.all_symbols())
    bars: dict[str, Any] = {}
    for _n, (broker, batch) in reg.by_broker(syms).items():
        try:
            bars.update(broker.get_bars(batch, 420))
        except BrokerError as e:
            log.warning("bars for %s unavailable: %s", batch[:3], e)
    try:
        funds = fu.fetch([s for s in syms if "/" not in s])
    except Exception:  # noqa: BLE001
        funds = {}
    try:
        signals = sm.collect()
    except Exception:  # noqa: BLE001
        signals = {}
    return score_mod.score_universe(bars, fundamentals=funds,
                                    sentiment=None, smart_money=signals)


@app.get("/api/basket")
def recommended_basket(follow: str | None = None,
                       _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    What to hold, in plain words. `follow` copies a named portfolio instead,
    filtered through the same screen and the same limits.
    """
    try:
        board = trackers.build()
    except Exception as e:  # noqa: BLE001 - the basket works without the tilt
        log.warning("tracker tilt unavailable: %s", e)
        board = None
    try:
        scores = _score_universe()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"could not score the universe: {e}") from e

    result = basket_mod.build(scores, tracker_board=board, follow=follow)
    return result.to_dict() | {"followable": basket_mod.followable(board)}


class FollowBasket(BaseModel):
    follow: str | None = None


@app.post("/api/basket/queue")
def queue_basket(body: FollowBasket, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Turn the basket into proposals in the approval queue.

    Each holding goes through manual.submit, which means the same risk gate and
    the same single path to the broker as everything else. Nothing is bought
    here; it still needs an approval per line.
    """
    reg = registry()
    try:
        account = next(iter(reg.active().values())).get_account()
        positions = {p.symbol.upper(): p
                     for b in reg.active().values() for p in b.get_positions()}
    except (BrokerError, StopIteration) as e:
        raise HTTPException(502, f"broker unavailable: {e}") from e

    try:
        board = trackers.build()
    except Exception:  # noqa: BLE001
        board = None
    result = basket_mod.build(_score_universe(), tracker_board=board,
                              follow=body.follow)

    queued, rejected = [], []
    for pick in result.picks:
        current = float(getattr(positions.get(pick.symbol), "weight", 0.0) or 0.0)
        if abs(pick.weight - current) < 1e-6:
            continue
        decision = manual.submit(
            manual.Request(
                symbol=pick.symbol,
                side="BUY" if pick.weight > current else "SELL",
                target_weight=pick.weight,
                reason=pick.why,
                exit_rule=("Leaves the basket when it no longer clears the "
                           "screen on the next rebuild."),
                invalidation=("The reason it was picked stops being true: the "
                              "score falls below the basket threshold."),
            ),
            account=account, positions=positions, store=store,
            held_days={pick.symbol: store.held_days(pick.symbol)},
        )
        (queued if decision.accepted else rejected).append(
            {"symbol": pick.symbol, "weight": pick.weight,
             "reasons": decision.reasons})

    store.log_execution({"event": "basket_queued", "following": body.follow,
                         "queued": len(queued), "rejected": len(rejected)})
    return {"queued": queued, "rejected": rejected,
            "note": (f"{len(queued)} lines are waiting for approval. "
                     f"{len(rejected)} were refused by the risk gate.")}


@app.get("/api/connections")
def list_connections(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Supported brokers and what each can do, plus whatever is connected."""
    return {"catalog": connections.catalog(),
            "connections": connections.listing()}


class NewConnection(BaseModel):
    broker: str
    label: str = ""
    mode: str = "paper"
    credentials: dict[str, str] = {}


@app.post("/api/connections")
def add_connection(body: NewConnection,
                   _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Store a connection and immediately try it, so a typo is reported now
    rather than at the next scheduled run.
    """
    try:
        conn = connections.add(body.broker, body.label, body.mode,
                               body.credentials)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    result = connections.test(conn)
    store.log_execution({"event": "connection_added", "broker": body.broker,
                         "mode": body.mode, "ok": result.get("ok")})
    return {"connection": conn.public(), "test": result}


class EditConnection(BaseModel):
    mode: str | None = None
    enabled: bool | None = None
    label: str | None = None


@app.patch("/api/connections/{conn_id}")
def edit_connection(conn_id: str, body: EditConnection,
                    _: bool = Depends(require_auth)) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        conn = connections.update(conn_id, **changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not conn:
        raise HTTPException(404, "no such connection")
    if "mode" in changes:
        store.log_execution({"event": "connection_mode_changed",
                             "broker": conn.broker, "mode": conn.mode})
    return conn.public()


@app.post("/api/connections/{conn_id}/test")
def test_connection(conn_id: str, _: bool = Depends(require_auth)) -> dict[str, Any]:
    conn = connections.get(conn_id)
    if not conn:
        raise HTTPException(404, "no such connection")
    return connections.test(conn)


@app.delete("/api/connections/{conn_id}")
def delete_connection(conn_id: str, _: bool = Depends(require_auth)) -> dict[str, bool]:
    if not connections.remove(conn_id):
        raise HTTPException(404, "no such connection")
    return {"removed": True}


class ImportRequest(BaseModel):
    text: str | None = None
    connection_id: str | None = None


@app.post("/api/import")
def import_portfolio(body: ImportRequest,
                     _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Read a portfolio, from a connected broker or from pasted text, and compare
    it to the recommended basket. Nothing is bought and nothing is stored on
    the broker; this is a comparison.
    """
    positions: list[dict[str, Any]] = []
    unparsed: list[str] = []
    source = "paste"

    if body.connection_id:
        result = connections.holdings(body.connection_id)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "connection failed"))
        positions = result.get("positions", [])
        source = result.get("summary", "connected broker")
    elif body.text:
        positions, unparsed = reconcile.parse(body.text)
    if not positions:
        raise HTTPException(
            400, "nothing readable. Paste one holding per line as a symbol "
                 "then a quantity, for example: AAPL 10")

    try:
        board = trackers.build()
    except Exception:  # noqa: BLE001
        board = None
    scores = _score_universe()
    recommended = basket_mod.build(scores, tracker_board=board)

    quotes: dict[str, float] = {}
    try:
        reg = registry()
        wanted = [p["symbol"] for p in positions
                  if p["symbol"] in {s.upper() for s in config.all_symbols()}]
        for _n, (broker, batch) in reg.by_broker(wanted).items():
            for sym, q in broker.get_quotes(batch).items():
                quotes[sym] = q.price
    except BrokerError as e:
        log.warning("could not price imported holdings: %s", e)

    report = reconcile.build(positions, recommended, scores, prices=quotes)
    report.unparsed = unparsed
    return report.to_dict() | {"source": source,
                               "basket": recommended.to_dict()}


@app.get("/api/sectors")
def sector_map(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    The heatmap. Price for all eleven US sectors, disclosed flow for whichever
    of them the filing sources actually covered.
    """
    from src.data import fundamentals as fu
    try:
        signals = sm.collect()
    except Exception as e:  # noqa: BLE001 - the price layer still works alone
        log.warning("sector flow unavailable: %s", e)
        signals = {}
    try:
        funds = fu.fetch([s for s in config.all_symbols() if "/" not in s])
    except Exception:  # noqa: BLE001
        funds = {}
    return sectors_mod.build(signals=signals, fundamentals=funds)


@app.get("/api/sectors/{etf}")
def sector_detail(etf: str, window: str = "m3",
                  _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    One sector opened up: which holdings drove the move, which tracked
    managers own it, and who has been buying or selling inside it.
    """
    try:
        signals = sm.collect()
    except Exception:  # noqa: BLE001
        signals = {}
    try:
        return sectors_mod.detail(etf.upper(), window=window, signals=signals)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/trackers")
def tracker_board(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Disclosed portfolios ranked by measured excess over the benchmark.

    Slow to rebuild and cached for half a day. It reads several years of
    filings and prices every position in them.
    """
    try:
        board = trackers.build()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"tracker board unavailable: {e}") from e
    return board | {"consensus": trackers.top_holdings(board, 12)}


@app.get("/api/objective")
def objective_state(days: int = 260, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Whether the book is beating the benchmark, and by how much against target.
    """
    curve = equity_curve(days=days, _=True)
    edge = objective.evaluate(curve.get("points", []),
                              curve.get("benchmark", []))
    if edge is None:
        return {"available": False,
                "note": "not enough equity history to measure an edge yet"}
    return {"available": True} | edge.to_dict()


@app.get("/api/smart-money")
def smart_money(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Disclosed trades by insiders, members of Congress and tracked funds.

    Served from the on-disk cache, so this is fast and does not hammer the SEC
    on every dashboard poll. `collect` refreshes it on its own TTL.
    """
    try:
        signals = sm.collect()
    except Exception as e:  # noqa: BLE001
        log.warning("smart money unavailable: %s", e)
        raise HTTPException(503, f"smart money sources unavailable: {e}") from e
    return {
        "signals": {k: v.to_dict() for k, v in signals.items()},
        "feed": sm.recent_feed(signals, limit=60),
        "sources": config.load("smartmoney").get("sources", {}),
    }


class Convene(BaseModel):
    symbol: str


@app.post("/api/committee")
def run_committee(body: Convene, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Put one symbol to the committee. Slow on purpose: four models answer
    independently and then audit each other, which is several minutes of real
    inference, not a cached lookup.
    """
    symbol = body.symbol.upper().strip()
    if symbol not in {s.upper() for s in config.all_symbols()}:
        raise HTTPException(400, f"{symbol} is not in the universe")

    from src.data import fundamentals as fu
    reg = registry()
    try:
        quotes = {}
        for _n, (broker, syms) in reg.by_broker([symbol]).items():
            quotes.update(broker.get_quotes(syms))
    except BrokerError as e:
        log.warning("committee could not get a quote for %s: %s", symbol, e)
        quotes = {}

    try:
        signals = sm.collect([symbol])
    except Exception:  # noqa: BLE001
        signals = {}

    # A sector fund is judged on different evidence than a company. There are
    # no margins to read; what matters is which holdings are driving it, which
    # tracked managers own it, and whether the flow agrees with the price.
    sector_context = None
    if symbol in sectors_mod.SECTORS:
        try:
            sector_context = sectors_mod.detail(symbol, signals=signals)
        except Exception as e:  # noqa: BLE001
            log.warning("sector context for %s unavailable: %s", symbol, e)

    evidence = {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "instrument": "sector fund" if sector_context else "company",
        "sector_context": sector_context,
        "quote": ({"price": quotes[symbol].price,
                   "as_of": quotes[symbol].ts.isoformat(),
                   "stale": quotes[symbol].stale,
                   "age_hours": round(quotes[symbol].age_hours, 2)}
                  if quotes.get(symbol) else None),
        "fundamentals": {k: v.to_dict() for k, v in fu.fetch([symbol]).items()},
        "smart_money": signals[symbol].to_dict() if symbol in signals else None,
        "retrieved_context": rag.context_for(
            symbol, f"{symbol} outlook, risks, valuation and recent developments", k=6),
    }
    verdict = committee.convene(symbol, evidence)
    payload = verdict.to_dict()
    store.save_committee(symbol, payload)
    ingest.disclosures({symbol: signals[symbol]} if symbol in signals else {})
    ingest.committee(symbol, payload)
    return payload


@app.get("/api/committee/{symbol}")
def last_committee(symbol: str, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """The most recent verdict for a symbol, without paying to rerun it."""
    saved = store.committee(symbol.upper())
    if not saved:
        raise HTTPException(404, f"no committee has sat on {symbol.upper()} yet")
    return saved


@app.get("/api/rag")
def rag_state(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Corpus size and which embedder is actually loaded."""
    return rag.index().stats()


class ManualOrder(BaseModel):
    symbol: str
    side: str
    target_weight: float | None = None
    notional: float | None = None
    reason: str = ""
    exit_rule: str = ""
    invalidation: str = ""
    override_holding_period: bool = False


@app.post("/api/manual-order")
def manual_order(body: ManualOrder, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    Enter a trade by hand. It passes the same risk gate as a generated proposal
    and lands in the same approval queue, so there is still exactly one path to
    the broker.
    """
    reg = registry()
    try:
        equity = sum(b.get_account().equity for b in reg.active().values())
        account = next(iter(reg.active().values())).get_account()
        positions = {p.symbol.upper(): p
                     for b in reg.active().values() for p in b.get_positions()}
        quotes: dict[str, Any] = {}
        for _n, (broker, syms) in reg.by_broker([body.symbol.upper()]).items():
            quotes.update(broker.get_quotes(syms))
    except (BrokerError, StopIteration) as e:
        raise HTTPException(502, f"broker unavailable: {e}") from e

    decision = manual.submit(
        manual.Request(
            symbol=body.symbol, side=body.side,
            target_weight=body.target_weight, notional=body.notional,
            reason=body.reason, exit_rule=body.exit_rule,
            invalidation=body.invalidation,
            override_holding_period=body.override_holding_period,
        ),
        account=account, positions=positions, quotes=quotes, store=store,
        held_days={body.symbol.upper(): store.held_days(body.symbol.upper())},
    )
    if decision.accepted:
        store.log_execution({"event": "manual_order_queued",
                             "symbol": body.symbol.upper(), "side": body.side})
    return decision.to_dict()


@app.get("/api/autopilot")
def autopilot_state(_: bool = Depends(require_auth)) -> dict[str, Any]:
    cfg = config.risk().get("autopilot", {})
    return {
        "armed": store.autopilot(),
        "enabled_in_config": bool(cfg.get("enabled", False)),
        "trades_today": store.auto_trades_today(),
        "max_trades_per_day": cfg.get("max_trades_per_day"),
        "min_materiality": cfg.get("min_materiality"),
        "min_confidence": cfg.get("min_confidence"),
        "max_notional_per_trade_pct": cfg.get("max_notional_per_trade_pct"),
        "kelly_fraction": config.risk().get("kelly", {}).get("fraction"),
    }


class Arm(BaseModel):
    armed: bool


@app.post("/api/autopilot")
def set_autopilot(body: Arm, _: bool = Depends(require_auth)) -> dict[str, Any]:
    """
    The kill switch. Disarming takes effect on the next event check; nothing
    is queued in a way that could fire after you switch it off.
    """
    store.set_autopilot(body.armed)
    store.log_execution({"event": "autopilot_armed" if body.armed else "autopilot_disarmed"})
    return {"armed": store.autopilot()}


@app.post("/api/scan-news")
def scan_news(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Run the event path now instead of waiting for the next poll."""
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "run_event.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "news scan timed out") from None
    return {"ok": proc.returncode == 0, "log": (proc.stderr or proc.stdout)[-4000:]}


def _benchmark_series(reg: Registry, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The benchmark bought on the curve's first day, in the same dollars."""
    symbol = (config.universe().get("benchmark") or "").upper()
    if not symbol or len(points) < 2:
        return []
    try:
        bars = reg.for_symbol(symbol).get_bars([symbol], 260).get(symbol, [])
    except BrokerError as e:
        log.warning("benchmark bars unavailable: %s", e)
        return []
    if not bars:
        return []

    closes = {b.ts.date().isoformat(): b.close for b in bars}
    start_day, start_equity = points[0]["t"], points[0]["equity"]
    base = next((c for d, c in sorted(closes.items()) if d >= start_day), None)
    if not base or not start_equity:
        return []

    out: list[dict[str, Any]] = []
    last = base
    for p in points:
        prior = [c for d, c in sorted(closes.items()) if d <= p["t"]]
        last = prior[-1] if prior else last
        out.append({"t": p["t"], "equity": round(start_equity * last / base, 2)})
    return out


@app.get("/api/runs")
def runs(_: bool = Depends(require_auth)) -> dict[str, Any]:
    return {"runs": store.list_runs(limit=60)}


@app.get("/api/run/latest")
def latest_run(_: bool = Depends(require_auth)) -> dict[str, Any]:
    run = store.latest_run()
    if not run:
        raise HTTPException(404, "no runs yet - execute run_daily.py")
    return _slim(run)


@app.get("/api/run/{run_id}")
def get_run(run_id: str, _: bool = Depends(require_auth)) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"no run {run_id}")
    return _slim(run)


@app.get("/api/report/{run_id}")
def get_report(run_id: str, _: bool = Depends(require_auth)) -> dict[str, str]:
    path = ROOT / "reports" / f"{run_id}.md"
    if not path.exists():
        raise HTTPException(404, f"no report for {run_id}")
    return {"run_id": run_id, "markdown": path.read_text(encoding="utf-8")}


@app.get("/api/executions")
def executions(limit: int = 50, _: bool = Depends(require_auth)) -> dict[str, Any]:
    return {"executions": store.executions(limit=limit)}


@app.get("/api/orders")
def orders(limit: int = 50, _: bool = Depends(require_auth)) -> dict[str, Any]:
    reg = registry()
    out: list[dict[str, Any]] = []
    for name, broker in reg.active().items():
        try:
            out.extend(o.to_dict() for o in broker.get_orders(limit=limit))
        except BrokerError as e:
            log.warning("orders failed on %s: %s", name, e)
    return {"orders": sorted(out, key=lambda o: o["submitted_at"], reverse=True)[:limit]}


def _slim(run: dict[str, Any]) -> dict[str, Any]:
    """The dashboard never needs the full per-symbol feature dump."""
    out = {k: v for k, v in run.items() if k != "scores"}
    out["scores"] = {
        s: {"total": v["total"], "components": v["components"],
            "eligible": v["eligible"], "unscored": v["unscored"],
            "asset_class": v["asset_class"]}
        for s, v in run.get("scores", {}).items()
    }
    return out


# --------------------------------------------------------------------------
# the only write path
# --------------------------------------------------------------------------

class Decision(BaseModel):
    note: str = ""


@app.post("/api/proposals/{proposal_id:path}/reject")
def reject(proposal_id: str, body: Decision, _: bool = Depends(require_auth)) -> dict[str, Any]:
    run_id = proposal_id.split(":", 1)[0]
    p = store.set_proposal_status(run_id, proposal_id, REJECTED, note=body.note)
    store.log_execution({"event": "rejected", "proposal_id": proposal_id,
                         "symbol": p["symbol"], "note": body.note})
    return {"ok": True, "proposal": p}


@app.post("/api/proposals/{proposal_id:path}/approve")
def approve(proposal_id: str, body: Decision, _: bool = Depends(require_auth)) -> dict[str, Any]:
    run_id = proposal_id.split(":", 1)[0]
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"no run {run_id}")
    proposal = next((p for p in run["proposals"] if p["id"] == proposal_id), None)
    if proposal is None:
        raise HTTPException(404, f"no proposal {proposal_id}")
    if proposal["status"] != PENDING:
        raise HTTPException(409, f"proposal is already {proposal['status']}")

    reg = registry()
    broker = reg.for_symbol(proposal["symbol"])

    # Re-check the gate against live numbers. The memo may be hours old.
    try:
        account = broker.get_account()
        live_quotes = broker.get_quotes([proposal["symbol"]])
    except BrokerError as e:
        raise HTTPException(502, f"broker unreachable: {e}") from e

    verdict = risk.evaluate([proposal], account=account, quotes=live_quotes,
                            broker_mode=broker.mode)
    ok, why = risk.approvable(proposal, verdict)
    if not ok:
        store.set_proposal_status(run_id, proposal_id, FAILED, note=f"risk re-check: {why}")
        raise HTTPException(409, f"risk gate now blocks this: {why}")

    if broker.mode != "paper":
        raise HTTPException(403, "this build only sends orders to paper accounts")

    # Size against live equity, not the equity the memo was written with.
    delta_usd = proposal["target_weight"] * account.equity - _held_value(broker, proposal["symbol"])
    if abs(delta_usd) < 1.0:
        store.set_proposal_status(run_id, proposal_id, EXECUTED,
                                  note="no order needed, position already at target")
        return {"ok": True, "skipped": True,
                "reason": "position is already within $1 of the target weight"}

    side = "buy" if delta_usd > 0 else "sell"
    order_req = OrderRequest(
        symbol=proposal["symbol"], side=side, notional=abs(round(delta_usd, 2)),
        asset_class=proposal["asset_class"],
        client_order_id=f"pm-{proposal_id.replace(':', '-')}"[:48],
    )

    try:
        result = broker.submit_order(order_req)
    except BrokerError as e:
        store.set_proposal_status(run_id, proposal_id, FAILED, note=str(e))
        store.log_execution({"event": "failed", "proposal_id": proposal_id,
                             "symbol": proposal["symbol"], "error": str(e)})
        raise HTTPException(502, f"broker rejected the order: {e}") from e

    store.set_proposal_status(run_id, proposal_id, EXECUTED,
                              note=body.note or "approved in dashboard",
                              order=result.to_dict())
    store.log_execution({
        "event": "executed", "proposal_id": proposal_id, "symbol": proposal["symbol"],
        "side": side, "notional": round(abs(delta_usd), 2), "broker": broker.name,
        "order": result.to_dict(), "action": proposal["action"],
        "target_weight": proposal["target_weight"], "run_id": run_id,
    })
    return {"ok": True, "order": result.to_dict()}


@app.post("/api/run")
def trigger_run(_: bool = Depends(require_auth)) -> dict[str, Any]:
    """Run the daily pipeline now. Bounded so a hung LLM call cannot wedge the box."""
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "run_daily.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "run timed out after 10 minutes") from None
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "log": (proc.stderr or proc.stdout)[-4000:],
    }


def _held_value(broker: Any, symbol: str) -> float:
    try:
        return next((p.market_value for p in broker.get_positions()
                     if p.symbol.upper() == symbol.upper()), 0.0)
    except BrokerError:
        return 0.0


# --------------------------------------------------------------------------
# static
# --------------------------------------------------------------------------

@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """
    The dashboard is a single unversioned bundle, so a cached app.js after a
    deploy shows old numbers with a fresh API behind them. Revalidate the shell
    on every load. These files are a few KB, the round trip is cheap, and being
    wrong here is silent.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _asset_stamp() -> str:
    """Newest mtime across the frontend files, as a short hex build id."""
    newest = 0.0
    for name in ("app.js", "sphere.js", "styles.css", "index.html"):
        f = WEB_DIR / name
        if f.exists():
            newest = max(newest, f.stat().st_mtime)
    return format(int(newest), "x")


def _index_html() -> HTMLResponse:
    """
    Serve index.html with the asset URLs stamped by build time.

    Cache-Control alone was not enough: a browser that cached app.js before the
    header existed keeps serving it from a heuristic freshness window, so a
    deploy silently runs old JavaScript against a new API. A changing URL
    cannot be served from cache at all.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stamp = _asset_stamp()
    html = html.replace('href="/styles.css"', f'href="/styles.css?v={stamp}"')
    html = html.replace('src="/app.js"', f'src="/app.js?v={stamp}"')
    html = html.replace('src="/sphere.js"', f'src="/sphere.js?v={stamp}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return _index_html()


@app.exception_handler(404)
async def spa_fallback(request: Any, exc: Any) -> Any:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": getattr(exc, "detail", "not found")}, status_code=404)
    return _index_html()


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=False), name="web")
