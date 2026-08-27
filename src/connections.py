"""
Connecting a broker, and being honest about what each one can do.

The point of this module is that a person should be able to paste two fields
into a form and see their holdings, without reading a YAML file or restarting
anything. What it must not do is pretend the brokers are interchangeable,
because they are not, and the differences are the sort that lose money:

  Alpaca       Reads positions from either account. Paper and live are
               separate accounts with separate keys and separate URLs, so
               switching mode is not a flag on one account, it is a different
               account. See the note at the end about why this does not also
               mean orders go there.

  Hyperliquid  Positions are public: anyone can read any address, and no key
               is involved. Trading needs an EVM private key, which is a
               fundamentally different object from an API key. An exchange key
               with trading permission can be revoked and cannot withdraw. A
               private key can move the funds. This module reads by address
               and does not accept a private key.

  Groww        Real Indian brokerage, read and trade through their official
               API. There is no paper mode. Anything sent is real, which is
               why this connects read only.

The capability table below is what the interface renders. A broker that cannot
do something says so in the table rather than failing later with a stack trace
the person cannot act on.

Credentials live in data/connections.json with owner-only permissions, outside
the repo and outside the image. They are never returned by the API: the UI gets
a masked hint and nothing else, so a leaked dashboard session cannot exfiltrate
the keys behind it.

WHAT A CONNECTION DOES NOT DO, and this is the important part:

A connection is read only, for every broker, including Alpaca. Orders are
placed through the broker configured in config/brokers.yaml and nowhere else.
Adding a live Alpaca connection here lets the app read that account's
positions; it does not route orders to it.

That separation is deliberate. Credentials pasted into a web form are easy to
add and easy to add by mistake, and a system where doing so silently changes
where real orders go is a system that will eventually send one to the wrong
account. Moving execution to a different broker is a config change and a
redeploy, which is slow on purpose.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .config import DATA_DIR

log = logging.getLogger(__name__)

STORE = DATA_DIR / "connections.json"

PAPER, LIVE = "paper", "live"


@dataclass
class Field:
    key: str
    label: str
    secret: bool = True
    placeholder: str = ""
    help: str = ""


@dataclass
class BrokerSpec:
    key: str
    label: str
    #: what it can actually do, rendered in the interface
    can_read: bool
    can_trade: bool
    has_paper: bool
    modes: list[str]
    fields: list[Field]
    note: str
    signup: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fields"] = [asdict(f) for f in self.fields]
        return d


CATALOG: dict[str, BrokerSpec] = {
    "alpaca": BrokerSpec(
        key="alpaca", label="Alpaca",
        can_read=True, can_trade=False, has_paper=True,
        modes=[PAPER, LIVE],
        fields=[
            Field("key_id", "API key ID", secret=False,
                  placeholder="PK...",
                  help="Paper keys begin PK, live keys begin AK. If yours "
                       "begins CK it is an OAuth app credential, which is a "
                       "different thing and will not work here."),
            Field("secret_key", "Secret key", secret=True),
        ],
        note="US stocks, ETFs and crypto. Paper and live are separate accounts "
             "with separate keys, so switching mode means pasting the other "
             "account's keys rather than flipping a switch. Mode selects which "
             "account is read: connections are read only, and orders go to the "
             "broker in brokers.yaml regardless of what is connected here.",
        signup="https://app.alpaca.markets/paper/dashboard/overview",
    ),
    "hyperliquid": BrokerSpec(
        key="hyperliquid", label="Hyperliquid",
        can_read=True, can_trade=False, has_paper=True,
        modes=[PAPER, LIVE],
        fields=[
            Field("address", "Wallet address", secret=False,
                  placeholder="0x...",
                  help="Your public address. Positions on Hyperliquid are "
                       "public, so this is all that is needed to read them."),
        ],
        note="Read only, and deliberately. Trading on Hyperliquid is signed "
             "with an EVM private key rather than an API key: a key that can "
             "move your funds and cannot be scoped to trading alone. This "
             "system will not hold one.",
        signup="https://app.hyperliquid.xyz/",
    ),
    "groww": BrokerSpec(
        key="groww", label="Groww",
        can_read=True, can_trade=False, has_paper=False,
        modes=[LIVE],
        fields=[
            Field("api_key", "API key", secret=False),
            Field("api_secret", "API secret", secret=True),
        ],
        note="Indian equities, read only. Groww has no paper mode, so every "
             "order placed through it would be real money. Holdings are "
             "imported for advice; nothing is sent.",
        signup="https://groww.in/trade-api",
    ),
    "manual": BrokerSpec(
        key="manual", label="Paste it in",
        can_read=True, can_trade=False, has_paper=False,
        modes=[PAPER],
        fields=[],
        note="For any broker without an API, or when you would rather not "
             "connect one. Paste symbols and quantities and the advice works "
             "the same way.",
    ),
}


@dataclass
class Connection:
    id: str
    broker: str
    label: str
    mode: str = PAPER
    enabled: bool = True
    created: str = ""
    last_ok: str = ""
    last_error: str = ""
    #: never serialised to the API
    credentials: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        """
        What the interface is allowed to see. Credentials become a hint of the
        last four characters so a person can tell two keys apart without the
        dashboard ever holding either of them.
        """
        spec = CATALOG.get(self.broker)
        return {
            "id": self.id, "broker": self.broker, "label": self.label,
            "mode": self.mode, "enabled": self.enabled,
            "created": self.created, "last_ok": self.last_ok,
            "last_error": self.last_error,
            "hints": {k: ("…" + v[-4:] if len(v) > 4 else "set")
                      for k, v in self.credentials.items() if v},
            "can_trade": bool(spec and spec.can_trade),
            "has_paper": bool(spec and spec.has_paper),
        }


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def _load() -> list[Connection]:
    if not STORE.exists():
        return []
    try:
        rows = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("connections store unreadable: %s", e)
        return []
    return [Connection(**r) for r in rows if isinstance(r, dict)]


def _save(rows: list[Connection]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps([asdict(r) for r in rows], indent=2),
                     encoding="utf-8")
    try:
        os.chmod(STORE, stat.S_IRUSR | stat.S_IWUSR)   # owner only
    except OSError as e:
        log.warning("could not restrict permissions on %s: %s", STORE, e)


def catalog() -> list[dict[str, Any]]:
    return [s.to_dict() for s in CATALOG.values()]


def listing() -> list[dict[str, Any]]:
    return [c.public() for c in _load()]


def get(conn_id: str) -> Connection | None:
    return next((c for c in _load() if c.id == conn_id), None)


def add(broker: str, label: str, mode: str,
        credentials: dict[str, str]) -> Connection:
    spec = CATALOG.get(broker)
    if not spec:
        raise ValueError(f"unknown broker {broker!r}")
    if mode not in spec.modes:
        raise ValueError(
            f"{spec.label} does not support {mode} mode"
            + ("; it has no paper account" if mode == PAPER else ""))

    missing = [f.key for f in spec.fields if not credentials.get(f.key)]
    if missing:
        raise ValueError(f"missing: {', '.join(missing)}")

    conn = Connection(
        id=uuid.uuid4().hex[:12], broker=broker,
        label=label.strip() or spec.label, mode=mode,
        created=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        credentials={f.key: str(credentials[f.key]).strip()
                     for f in spec.fields if credentials.get(f.key)},
    )
    rows = _load()
    rows.append(conn)
    _save(rows)
    return conn


def update(conn_id: str, **changes: Any) -> Connection | None:
    rows = _load()
    for c in rows:
        if c.id != conn_id:
            continue
        if "mode" in changes:
            spec = CATALOG.get(c.broker)
            if spec and changes["mode"] not in spec.modes:
                raise ValueError(f"{spec.label} does not support {changes['mode']} mode")
            c.mode = changes["mode"]
        if "enabled" in changes:
            c.enabled = bool(changes["enabled"])
        if "label" in changes and changes["label"]:
            c.label = str(changes["label"]).strip()
        _save(rows)
        return c
    return None


def remove(conn_id: str) -> bool:
    rows = _load()
    kept = [c for c in rows if c.id != conn_id]
    if len(kept) == len(rows):
        return False
    _save(kept)
    return True


def _mark(conn_id: str, ok: bool, error: str = "") -> None:
    rows = _load()
    for c in rows:
        if c.id == conn_id:
            if ok:
                c.last_ok = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                c.last_error = ""
            else:
                c.last_error = error[:240]
            _save(rows)
            return


# --------------------------------------------------------------------------
# talking to the brokers
# --------------------------------------------------------------------------

def test(conn: Connection) -> dict[str, Any]:
    """
    Verify a connection and return a short summary. Never raises: a failure is
    a result the interface renders, not a stack trace.
    """
    try:
        result = _probe(conn)
        _mark(conn.id, True)
        return {"ok": True} | result
    except Exception as e:  # noqa: BLE001
        message = f"{type(e).__name__}: {e}"
        _mark(conn.id, False, message)
        return {"ok": False, "error": message[:240]}


def _probe(conn: Connection) -> dict[str, Any]:
    if conn.broker == "alpaca":
        return _alpaca(conn)
    if conn.broker == "hyperliquid":
        return _hyperliquid(conn)
    if conn.broker == "groww":
        return _groww(conn)
    if conn.broker == "manual":
        return {"summary": "manual entry, nothing to connect", "positions": []}
    raise ValueError(f"no probe for {conn.broker}")


def _alpaca(conn: Connection) -> dict[str, Any]:
    import requests
    base = ("https://paper-api.alpaca.markets" if conn.mode == PAPER
            else "https://api.alpaca.markets")
    headers = {"APCA-API-KEY-ID": conn.credentials.get("key_id", ""),
               "APCA-API-SECRET-KEY": conn.credentials.get("secret_key", "")}

    acct = requests.get(f"{base}/v2/account", headers=headers, timeout=20)
    if acct.status_code == 401:
        raise PermissionError(
            "Alpaca rejected these credentials. Check the key belongs to the "
            f"{conn.mode} account: paper keys begin PK and live keys begin AK. "
            "A key beginning CK is an OAuth app credential and will not work.")
    acct.raise_for_status()
    a = acct.json()

    pos = requests.get(f"{base}/v2/positions", headers=headers, timeout=20)
    pos.raise_for_status()
    holdings = [
        {"symbol": p["symbol"], "qty": float(p["qty"]),
         "market_value": float(p.get("market_value") or 0),
         "cost_basis": float(p.get("cost_basis") or 0)}
        for p in pos.json()
    ]
    return {
        "summary": (f"{conn.mode} account, equity "
                    f"${float(a.get('equity') or 0):,.2f}, "
                    f"{len(holdings)} positions"),
        "equity": float(a.get("equity") or 0),
        "positions": holdings,
    }


def _hyperliquid(conn: Connection) -> dict[str, Any]:
    import requests
    base = ("https://api.hyperliquid-testnet.xyz" if conn.mode == PAPER
            else "https://api.hyperliquid.xyz")
    address = conn.credentials.get("address", "")
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError("that does not look like a wallet address; it should "
                         "be 0x followed by 40 characters")

    r = requests.post(f"{base}/info", timeout=20,
                      json={"type": "clearinghouseState", "user": address})
    r.raise_for_status()
    state = r.json()

    holdings = []
    for p in state.get("assetPositions", []):
        item = p.get("position") or {}
        size = float(item.get("szi") or 0)
        if size:
            holdings.append({
                "symbol": item.get("coin", "?"),
                "qty": size,
                "market_value": float(item.get("positionValue") or 0),
                "cost_basis": float(item.get("entryPx") or 0) * abs(size),
            })
    equity = float((state.get("marginSummary") or {}).get("accountValue") or 0)
    return {
        "summary": (f"{conn.mode} wallet, account value ${equity:,.2f}, "
                    f"{len(holdings)} open positions"),
        "equity": equity,
        "positions": holdings,
    }


def _groww(conn: Connection) -> dict[str, Any]:
    try:
        from growwapi import GrowwAPI
    except ImportError as e:
        raise RuntimeError(
            "the growwapi package is not installed in this image, so Groww "
            "cannot be reached. Add growwapi to requirements.txt and redeploy."
        ) from e

    token = GrowwAPI.get_access_token(
        api_key=conn.credentials.get("api_key", ""),
        secret=conn.credentials.get("api_secret", ""))
    client = GrowwAPI(token)
    raw = client.get_holdings_for_user(timeout=15) or {}
    rows = raw.get("holdings", raw if isinstance(raw, list) else [])

    holdings = []
    for h in rows:
        qty = float(h.get("quantity") or 0)
        if not qty:
            continue
        holdings.append({
            "symbol": h.get("trading_symbol") or h.get("symbol") or "?",
            "qty": qty,
            "market_value": float(h.get("current_value") or 0),
            "cost_basis": float(h.get("average_price") or 0) * qty,
        })
    return {
        "summary": f"live account, {len(holdings)} holdings",
        "equity": sum(h["market_value"] for h in holdings),
        "positions": holdings,
    }


def holdings(conn_id: str) -> dict[str, Any]:
    """Positions from one connection, for the import view."""
    conn = get(conn_id)
    if not conn:
        return {"ok": False, "error": "no such connection"}
    if not conn.enabled:
        return {"ok": False, "error": "this connection is switched off"}
    return test(conn)
