"""
The US sector map: what each part of the market is doing, and who is buying it.

Eleven sectors, each with the SPDR ETF that tracks it. The ETF matters. A
heatmap of abstract sector labels tells you where the market moved; a heatmap
keyed to tickers tells you what you could actually have bought, and the second
is the only one worth putting in front of somebody.

Two independent layers sit on each tile:

  Price      Return over several windows, annualised volatility, and distance
             from the 50 and 200 day averages. This is what happened.

  Flow       Net disclosed dollars from the three filing sources, attributed to
             a sector through each holding's own classification. This is where
             institutional money, Congress and insiders have been moving.

They are kept apart on purpose. Price momentum and disclosed flow disagree
often, and the disagreement is the interesting part: a sector the funds are
buying into weakness reads differently from one they are chasing. Blending them
into a single number would hide exactly the case worth looking at.

The India sectors that the original standalone heatmap carried are deliberately
gone. This portfolio trades a US universe through a US broker, and a heatmap
tile you cannot act on is decoration.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import statistics
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from .. import config
from ..config import DATA_DIR

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "sectors"
BENCHMARK = "SPY"

# The eleven SPDR select sector funds. `yf` lists the sector names Yahoo
# reports for individual companies, which is how a holding is attributed to a
# tile. Yahoo's vocabulary is not GICS and does not match the ETF names, which
# is why the mapping is written out rather than assumed.
SECTORS: dict[str, dict[str, Any]] = {
    "XLK":  {"name": "Information Technology", "yf": ["Technology"]},
    "XLC":  {"name": "Communication Services", "yf": ["Communication Services"]},
    "XLY":  {"name": "Consumer Discretionary", "yf": ["Consumer Cyclical"]},
    "XLP":  {"name": "Consumer Staples",       "yf": ["Consumer Defensive"]},
    "XLE":  {"name": "Energy",                 "yf": ["Energy"]},
    "XLF":  {"name": "Financials",             "yf": ["Financial Services"]},
    "XLV":  {"name": "Health Care",            "yf": ["Healthcare"]},
    "XLI":  {"name": "Industrials",            "yf": ["Industrials"]},
    "XLB":  {"name": "Materials",              "yf": ["Basic Materials"]},
    "XLRE": {"name": "Real Estate",            "yf": ["Real Estate"]},
    "XLU":  {"name": "Utilities",              "yf": ["Utilities"]},
}

# Fallback for holdings with no Yahoo sector, using the SIC derived taxonomy
# that the whale tracker built from EDGAR filings.
SIC_TO_SECTOR = {
    "Software & IT": "XLK", "Tech hardware": "XLK",
    "Telecom & media": "XLC",
    "Consumer retail": "XLY", "Autos & aerospace": "XLY",
    "Consumer goods": "XLP", "Food & beverage": "XLP",
    "Energy": "XLE",
    "Banks": "XLF", "Capital markets": "XLF", "Insurance": "XLF",
    "Biotech & pharma": "XLV", "Healthcare services": "XLV",
    "Medical devices": "XLV",
    "Industrials": "XLI", "Transport & logistics": "XLI",
    "Electrical equipment": "XLI", "Construction": "XLI",
    "Chemicals": "XLB", "Metals & mining": "XLB",
    "Real estate": "XLRE",
    "Utilities": "XLU",
}

WINDOWS = {"m1": 21, "m3": 63, "m6": 126, "y1": 252}


@dataclass
class SectorTile:
    etf: str
    name: str
    price: float = 0.0
    as_of: str = ""

    # price layer
    ytd: float | None = None
    m1: float | None = None
    m3: float | None = None
    m6: float | None = None
    y1: float | None = None
    vol: float | None = None
    vs50: float | None = None
    vs200: float | None = None
    spark: list[float] = field(default_factory=list)

    # flow layer, in dollars
    fund_net: float = 0.0
    congress_net: float = 0.0
    insider_net: float = 0.0
    flow_names: int = 0
    top_buys: list[dict[str, Any]] = field(default_factory=list)
    top_sells: list[dict[str, Any]] = field(default_factory=list)

    note: str = ""

    @property
    def net_flow(self) -> float:
        return self.fund_net + self.congress_net + self.insider_net

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["net_flow"] = round(self.net_flow, 2)
        return d


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

_YF_TO_ETF: dict[str, str] = {}
for _etf, _spec in SECTORS.items():
    for _name in _spec["yf"]:
        _YF_TO_ETF[_name] = _etf


def classify(symbol: str, fundamentals: dict[str, Any] | None = None) -> str | None:
    """
    Which sector tile a holding belongs to.

    Yahoo's own sector field first, because it is per company and current. The
    SIC table is the fallback for names Yahoo has nothing for. Anything that
    resolves to neither returns None and is counted as unclassified rather than
    being dropped into whichever tile happens to be nearest.
    """
    symbol = symbol.upper()
    if symbol in SECTORS:
        return symbol

    entry = (fundamentals or {}).get(symbol)
    yf_sector = getattr(entry, "sector", None) if entry is not None else None
    if not yf_sector and isinstance(entry, dict):
        yf_sector = entry.get("sector")
    if yf_sector and yf_sector in _YF_TO_ETF:
        return _YF_TO_ETF[yf_sector]

    sic = _sic_map().get(symbol, {}).get("sector")
    return SIC_TO_SECTOR.get(sic) if sic else None


_SIC_CACHE: dict[str, dict[str, Any]] | None = None


def _sic_map() -> dict[str, dict[str, Any]]:
    """The SIC derived ticker table, if it was shipped alongside the code."""
    global _SIC_CACHE
    if _SIC_CACHE is None:
        path = config.ROOT / "reference" / "sector_map.json"
        try:
            _SIC_CACHE = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.info("no sector_map.json, falling back to Yahoo sectors only")
            _SIC_CACHE = {}
    return _SIC_CACHE


# --------------------------------------------------------------------------
# price layer
# --------------------------------------------------------------------------

def _safe(key: str) -> str:
    """
    Cache keys become filenames, and some of the symbols reaching them come
    from outside: the ETF holdings list is whatever the fund publishes. A class
    B ticker carrying a slash (BRK/B) would open a subdirectory, and a crafted
    one with dot segments would write outside the cache directory entirely.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key).replace("..", "_")


def _cache_get(key: str, ttl: float) -> Any | None:
    path = CACHE_DIR / f"{_safe(key)}.json"
    if not path.exists() or time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (CACHE_DIR / f"{_safe(key)}.json").write_text(json.dumps(value, default=str),
                                               encoding="utf-8")
    except OSError as e:
        log.warning("could not cache %s: %s", key, e)


def _closes(symbol: str) -> list[tuple[str, float]]:
    """Two years of daily closes. Cached for the trading day."""
    cached = _cache_get(f"closes_{symbol}", ttl=6 * 3600)
    if cached is not None:
        return [(d, c) for d, c in cached]

    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="2y", interval="1d",
                                         auto_adjust=True)
        rows = [(i.date().isoformat(), float(c))
                for i, c in zip(hist.index, hist["Close"])
                if c == c and c > 0]
    except Exception as e:  # noqa: BLE001 - a dead price feed is not fatal
        log.warning("no price history for %s: %s", symbol, e)
        return []

    _cache_put(f"closes_{symbol}", rows)
    return rows


def _ret(closes: list[tuple[str, float]], bars: int) -> float | None:
    if len(closes) <= bars:
        return None
    a, b = closes[-1 - bars][1], closes[-1][1]
    return round((b / a - 1) * 100, 2) if a > 0 else None


def _ytd(closes: list[tuple[str, float]]) -> float | None:
    if not closes:
        return None
    year = closes[-1][0][:4]
    prior = [c for d, c in closes if d[:4] < year]
    if not prior:
        return None
    return round((closes[-1][1] / prior[-1] - 1) * 100, 2)


def _vol(closes: list[tuple[str, float]], window: int = 63) -> float | None:
    if len(closes) < window + 1:
        return None
    px = [c for _, c in closes[-(window + 1):]]
    rets = [math.log(b / a) for a, b in zip(px, px[1:]) if a > 0]
    if len(rets) < 2:
        return None
    return round(statistics.pstdev(rets) * math.sqrt(252) * 100, 1)


def _vs_ma(closes: list[tuple[str, float]], window: int) -> float | None:
    if len(closes) < window:
        return None
    ma = sum(c for _, c in closes[-window:]) / window
    return round((closes[-1][1] / ma - 1) * 100, 2) if ma > 0 else None


def _price_layer(tile: SectorTile) -> None:
    closes = _closes(tile.etf)
    if not closes:
        tile.note = "no price history"
        return
    tile.price = round(closes[-1][1], 2)
    tile.as_of = closes[-1][0]
    tile.ytd = _ytd(closes)
    for key, bars in WINDOWS.items():
        setattr(tile, key, _ret(closes, bars))
    tile.vol = _vol(closes)
    tile.vs50 = _vs_ma(closes, 50)
    tile.vs200 = _vs_ma(closes, 200)
    # One point a week for a year, which is enough to read a shape at tile size.
    weekly = closes[-252:][::5]
    tile.spark = [round(c, 2) for _, c in weekly]


# --------------------------------------------------------------------------
# flow layer
# --------------------------------------------------------------------------

def _flow_layer(tiles: dict[str, SectorTile], signals: dict[str, Any],
                fundamentals: dict[str, Any] | None) -> int:
    """
    Attribute every disclosed trade to a sector. Returns how many symbols could
    not be classified, which the caller reports rather than swallows.
    """
    per_sector: dict[str, dict[str, float]] = {
        k: {"fund": 0.0, "congress": 0.0, "insider": 0.0} for k in tiles}
    names: dict[str, set[str]] = {k: set() for k in tiles}
    moves: dict[str, dict[str, float]] = {k: {} for k in tiles}
    unclassified = 0

    for symbol, signal in signals.items():
        etf = classify(symbol, fundamentals)
        if etf is None or etf not in tiles:
            unclassified += 1
            continue

        trades = getattr(signal, "trades", None)
        if trades is None and isinstance(signal, dict):
            trades = signal.get("trades", [])
        for t in trades or []:
            get = (lambda k: t.get(k)) if isinstance(t, dict) else (lambda k: getattr(t, k, None))
            source = get("source")
            if source not in per_sector[etf]:
                continue
            signed = float(get("value_usd") or 0) * (1 if get("direction") == "BUY" else -1)
            per_sector[etf][source] += signed
            names[etf].add(symbol)
            moves[etf][symbol] = moves[etf].get(symbol, 0.0) + signed

    for etf, tile in tiles.items():
        tile.fund_net = round(per_sector[etf]["fund"], 2)
        tile.congress_net = round(per_sector[etf]["congress"], 2)
        tile.insider_net = round(per_sector[etf]["insider"], 2)
        tile.flow_names = len(names[etf])
        ranked = sorted(moves[etf].items(), key=lambda kv: kv[1], reverse=True)
        tile.top_buys = [{"symbol": s, "net_usd": round(v, 2)}
                         for s, v in ranked if v > 0][:5]
        tile.top_sells = [{"symbol": s, "net_usd": round(v, 2)}
                          for s, v in reversed(ranked) if v < 0][:5]

    return unclassified


# --------------------------------------------------------------------------

def build(*, signals: dict[str, Any] | None = None,
          fundamentals: dict[str, Any] | None = None,
          ttl: float = 6 * 3600) -> dict[str, Any]:
    """
    The whole map. Price for every sector, flow for whatever the disclosure
    sources covered.

    Flow coverage is deliberately reported. The portfolio universe is a couple
    of dozen names, so most sectors will show no disclosed flow at all, and a
    tile reading zero because nobody filed must not look like a tile reading
    zero because the money is balanced.
    """
    cached = _cache_get("map", ttl)
    if cached is not None and signals is None:
        return cached

    tiles = {etf: SectorTile(etf=etf, name=spec["name"])
             for etf, spec in SECTORS.items()}
    for tile in tiles.values():
        _price_layer(tile)

    unclassified = 0
    if signals:
        unclassified = _flow_layer(tiles, signals, fundamentals)

    bench = _closes(BENCHMARK)
    benchmark = {
        "symbol": BENCHMARK,
        "price": round(bench[-1][1], 2) if bench else None,
        "ytd": _ytd(bench), "m1": _ret(bench, WINDOWS["m1"]),
        "m3": _ret(bench, WINDOWS["m3"]), "m6": _ret(bench, WINDOWS["m6"]),
        "y1": _ret(bench, WINDOWS["y1"]),
    }

    covered = sum(1 for t in tiles.values() if t.flow_names)
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "sectors": [t.to_dict() for t in
                    sorted(tiles.values(), key=lambda t: -(t.m3 or -999))],
        "flow_coverage": {
            "sectors_with_flow": covered,
            "sectors_total": len(tiles),
            "unclassified_symbols": unclassified,
            "note": (f"disclosed flow covers {covered} of {len(tiles)} sectors. "
                     f"A sector with no flow had no disclosures in the window, "
                     f"which is not the same as balanced buying and selling."),
        },
    }
    _cache_put("map", payload)
    return payload


def leaders(payload: dict[str, Any], window: str = "m3",
            n: int = 3) -> dict[str, list[str]]:
    """Best and worst sectors over one window, for a one line summary."""
    rows = [s for s in payload.get("sectors", []) if s.get(window) is not None]
    rows.sort(key=lambda s: s[window], reverse=True)
    return {
        "leaders": [f"{s['name']} {s[window]:+.1f}%" for s in rows[:n]],
        "laggards": [f"{s['name']} {s[window]:+.1f}%" for s in rows[-n:]],
    }

# --------------------------------------------------------------------------
# drill down
# --------------------------------------------------------------------------

def _holdings(etf: str, ttl: float = 7 * 24 * 3600) -> list[dict[str, Any]]:
    """
    The ETF's own top holdings and weights, from the fund's published data.

    Yahoo exposes the top ten and no more, so a sector's move is attributed
    across those ten and the remainder is reported as unattributed rather than
    silently spread over them.
    """
    cached = _cache_get(f"holdings_{etf}", ttl)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        table = yf.Ticker(etf).funds_data.top_holdings
        rows = [{"symbol": str(sym).upper(),
                 "name": str(r["Name"]),
                 "weight": round(float(r["Holding Percent"]), 6)}
                for sym, r in table.iterrows()]
    except Exception as e:  # noqa: BLE001
        log.warning("no published holdings for %s: %s", etf, e)
        rows = []
    _cache_put(f"holdings_{etf}", rows)
    return rows


def detail(etf: str, *, window: str = "m3",
           signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    One sector opened up: what moved it, and who owns it.

    Contribution is weight times return, which is the share of the sector's
    move each holding is responsible for. It is the honest version of "what is
    driving this": a name up 40% at a 1% weight moved the sector less than a
    name up 6% at a 14% weight, and only the contribution column shows that.
    """
    etf = etf.upper()
    if etf not in SECTORS:
        raise KeyError(f"{etf} is not one of the eleven sector funds")

    bars = WINDOWS.get(window, WINDOWS["m3"])
    holdings = _holdings(etf)

    drivers: list[dict[str, Any]] = []
    attributed = 0.0
    for h in holdings:
        closes = _closes(h["symbol"])
        ret = _ret(closes, bars)
        if ret is None:
            drivers.append(h | {"ret": None, "contribution": None})
            continue
        contribution = h["weight"] * ret
        attributed += h["weight"]
        drivers.append(h | {"ret": ret, "contribution": round(contribution, 3)})

    drivers.sort(key=lambda d: (d["contribution"] is not None, d["contribution"] or 0),
                 reverse=True)

    tile = SectorTile(etf=etf, name=SECTORS[etf]["name"])
    _price_layer(tile)

    return {
        "etf": etf,
        "name": SECTORS[etf]["name"],
        "window": window,
        "sector_return": getattr(tile, window, None),
        "drivers": drivers,
        "coverage": {
            "holdings_shown": len(holdings),
            "weight_attributed": round(attributed, 4),
            "note": (f"The fund publishes its ten largest holdings, which are "
                     f"{attributed * 100:.0f}% of the ETF. The rest of the "
                     f"sector's move comes from positions not shown here."),
        },
        "investors": sector_investors(etf),
        "flow": sector_flow(etf, signals),
    }


def sector_investors(etf: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """
    Which tracked managers have the most money in this sector, and whether
    their disclosed book has actually beaten the benchmark.

    The second half matters more than the first. Knowing a large fund is heavy
    in a sector is only interesting alongside whether that fund has been right,
    so the measured excess travels with the name.
    """
    from .. import trackers  # imported here: trackers reads this module back

    try:
        board = trackers.build()
    except Exception as e:  # noqa: BLE001
        log.warning("investor overlay unavailable: %s", e)
        return []

    rows: list[dict[str, Any]] = []
    for t in board.get("trackers", []):
        if t.get("kind") != "fund":
            continue
        weight = 0.0
        names: list[str] = []
        for h in t.get("holdings", []):
            if classify(h["symbol"]) == etf:
                weight += h.get("weight", 0.0)
                names.append(h["symbol"])
        if weight <= 0:
            continue
        rows.append({
            "name": t["name"],
            "book_weight": round(weight, 4),
            "positions": sorted(names)[:8],
            "mean_excess": t.get("mean_excess"),
            "beat_rate": t.get("beat_rate"),
            "as_of": t.get("as_of"),
        })

    rows.sort(key=lambda r: r["book_weight"], reverse=True)
    return rows[:limit]


def sector_flow(etf: str, signals: dict[str, Any] | None) -> dict[str, Any]:
    """Disclosed buying and selling inside this sector, by actor."""
    if not signals:
        return {"actors": [], "note": "no disclosure data supplied"}

    by_actor: dict[str, dict[str, Any]] = {}
    for symbol, signal in signals.items():
        if classify(symbol) != etf:
            continue
        trades = getattr(signal, "trades", None)
        if trades is None and isinstance(signal, dict):
            trades = signal.get("trades", [])
        for t in trades or []:
            get = (lambda k: t.get(k)) if isinstance(t, dict) else (lambda k: getattr(t, k, None))
            actor = str(get("actor") or "unknown")
            signed = float(get("value_usd") or 0) * (1 if get("direction") == "BUY" else -1)
            row = by_actor.setdefault(actor, {
                "actor": actor, "source": get("source"), "net_usd": 0.0,
                "symbols": set()})
            row["net_usd"] += signed
            row["symbols"].add(symbol)

    actors = [{"actor": r["actor"], "source": r["source"],
               "net_usd": round(r["net_usd"], 2),
               "symbols": sorted(r["symbols"])}
              for r in by_actor.values()]
    actors.sort(key=lambda r: abs(r["net_usd"]), reverse=True)
    return {"actors": actors[:12],
            "note": "net disclosed dollars per actor inside this sector"}
