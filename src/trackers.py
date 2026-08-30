"""
Which disclosed portfolios are actually worth copying.

The premise most copy-trading products rely on is that famous managers are
worth following. That is an assumption, and it is testable, so this module
tests it instead: reconstruct each filer's disclosed book from their own
filings, hold it over the period after it became public, and compare it to the
same money in SPY across identical dates.

Most will lose. That is the expected result and it is the point. A leaderboard
that only shows the winners is a survivorship machine, so every tracker that
gets measured stays on the board with its number, including the negative ones.

What this measures, precisely, because the distinction matters:

  For a fund, the return of the long US equity positions it disclosed on Form
  13F, value weighted as filed, from the filing date forward. It is not the
  fund's return. A 13F shows no shorts, no options exposure beyond what is
  reported, no cash, no bonds, and nothing held outside the US. A market
  neutral fund can look terrible here while making money, and a levered long
  fund can look better than its investors did.

  For a congressional tracker, the return of the disclosed purchases from the
  date the filing became public, equal weighted. Members disclose a dollar
  range rather than an amount, so weighting by size would be inventing
  precision that the filing does not contain.

Both are measured from the date the information became public, never from the
trade date. Measuring from the trade date would credit the strategy with a
return nobody following it could have captured, which is how most published
tracker performance manages to look so good.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import statistics
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from . import config
from .config import DATA_DIR
from .data import smartmoney as sm

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "trackers"
BENCH = "SPY"
MAX_POSITIONS = 25          # a 13F tail of 400 names is index tracking

# A 13F is already up to 45 days stale when it lands. Past this the book is not
# a current view, and it is excluded from the consensus even when the manager's
# measured record is good.
MAX_BOOK_AGE_DAYS = 150

# A window has to price most of the book it claims to represent. Below this it
# is a handful of survivors wearing the manager's name: the weight of whatever
# could not be priced gets redistributed onto whatever could, which quietly
# deletes anything delisted at zero and reads three homebuilders as Berkshire.
MIN_COVERAGE = 0.60

# And enough names that the number is about a book rather than about one stock.
# Two disclosed purchases held for ninety days is a coin toss, not a record.
MIN_NAMES = 5

# One surviving window is one quarter, and one quarter is luck. A record needs
# at least a second one to be a record rather than an anecdote.
MIN_WINDOWS = 2


@dataclass
class Holding:
    symbol: str
    weight: float
    value_usd: float = 0.0
    #: share count as filed. The inverse trackers diff on this rather than on
    #: value, because a position nobody touched still changes in dollar value
    #: with the price, and a value diff would read that drift as a decision.
    shares: float = 0.0


@dataclass
class Window:
    """One measured period: what the book did against what SPY did."""
    label: str
    start: str
    end: str
    ret: float
    bench: float
    names: int
    #: fraction of the book's weight that could actually be priced over these
    #: dates. Anything under MIN_COVERAGE is not a measurement of this book.
    coverage: float = 1.0

    @property
    def excess(self) -> float:
        return round(self.ret - self.bench, 2)

    @property
    def days(self) -> int:
        return (dt.date.fromisoformat(self.end)
                - dt.date.fromisoformat(self.start)).days

    @property
    def measurable(self) -> bool:
        return self.coverage >= MIN_COVERAGE and self.names >= MIN_NAMES


@dataclass
class Tracker:
    key: str
    name: str
    kind: str                    # fund | congress
    source: str = ""
    as_of: str = ""
    holdings: list[Holding] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    beat_rate: float | None = None
    #: compounded book return minus compounded benchmark return over the whole
    #: measured span, in percentage points. Not an average of window excesses:
    #: the last window of every fund runs from its newest filing to today and
    #: is a fortnight rather than a quarter, and averaging gave that fortnight
    #: the same vote as a full quarter.
    excess: float | None = None
    span_days: int | None = None
    dropped_windows: int = 0
    latest_excess: float | None = None
    stale_days: int | None = None
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["windows"] = [asdict(w) | {"excess": w.excess} for w in self.windows]
        return d


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

_PRICES: dict[str, list[tuple[str, float]]] = {}


def _safe(key: str) -> str:
    """Class B tickers carry a slash (LEN/B), which would open a subdirectory."""
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


def prices(symbol: str) -> list[tuple[str, float]]:
    """Daily closes, memoised in process and cached on disk for the day."""
    symbol = symbol.upper()
    if symbol in _PRICES:
        return _PRICES[symbol]

    cached = _cache_get(f"px_{symbol}", ttl=12 * 3600)
    if cached is not None:
        _PRICES[symbol] = [(d, c) for d, c in cached]
        return _PRICES[symbol]

    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="3y", interval="1d",
                                         auto_adjust=True)
        rows = [(i.date().isoformat(), float(c))
                for i, c in zip(hist.index, hist["Close"]) if c == c and c > 0]
    except Exception as e:  # noqa: BLE001
        log.debug("no prices for %s: %s", symbol, e)
        rows = []

    _cache_put(f"px_{symbol}", rows)
    _PRICES[symbol] = rows
    return rows


def _ret(symbol: str, start: str, end: str) -> float | None:
    """
    Return between two dates, entering at the first close on or after `start`.

    Entering at the next available close rather than interpolating is what a
    person following this could actually have done.
    """
    rows = prices(symbol)
    if not rows:
        return None
    entry = next((c for d, c in rows if d >= start), None)
    exit_ = next((c for d, c in reversed(rows) if d <= end), None)
    if entry is None or exit_ is None or entry <= 0:
        return None
    return (exit_ / entry - 1) * 100


# --------------------------------------------------------------------------
# 13F books
# --------------------------------------------------------------------------

_CUSIPS: dict[str, dict[str, Any]] | None = None


def cusip_map() -> dict[str, dict[str, Any]]:
    global _CUSIPS
    if _CUSIPS is None:
        try:
            _CUSIPS = json.loads(
                (config.ROOT / "reference" / "cusip_map.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("no cusip_map.json, 13F books cannot be priced")
            _CUSIPS = {}
    return _CUSIPS


def _book(cik: str, accession: str) -> list[Holding]:
    """
    One filing's long book as weights, resolved to tickers by CUSIP.

    CUSIP rather than issuer name because the name field is free text: the same
    company appears as "ALPHABET INC", "ALPHABET INC CL A" and "ALPHABET INC
    CAP STK CL A" across three filers in the same quarter.
    """
    key = f"book_{cik}_{accession}"
    cached = _cache_get(key, ttl=365 * 24 * 3600)
    if cached is not None:
        return [Holding(**h) for h in cached]

    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    try:
        idx = sm._sec_get(f"{base}/index.json")
        idx.raise_for_status()
        names = [i["name"] for i in idx.json()["directory"]["item"]]
        table = next((n for n in names
                      if n.endswith(".xml") and "primary_doc" not in n), None)
        if not table:
            _cache_put(key, [])
            return []
        doc = sm._sec_get(f"{base}/{table}")
        doc.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(doc.content)
    except Exception as e:  # noqa: BLE001
        log.warning("13F book %s/%s unreadable: %s", cik, accession, e)
        return []

    cmap = cusip_map()
    by_symbol: dict[str, list[float]] = {}
    for node in root.iter():
        if not node.tag.endswith("infoTable"):
            continue
        cusip = value = shares = None
        for child in node.iter():
            if child.tag.endswith("cusip"):
                cusip = (child.text or "").strip()
            elif child.tag.endswith("value"):
                value = child.text
            elif child.tag.endswith("sshPrnamt"):
                shares = child.text
        if not cusip or value is None:
            continue
        info = cmap.get(cusip) or cmap.get(cusip.upper())
        if not info or not info.get("listed") or not info.get("ticker"):
            continue
        try:
            row = by_symbol.setdefault(info["ticker"], [0.0, 0.0])
            row[0] += float(value)
            row[1] += float(shares or 0)
        except ValueError:
            continue

    if not by_symbol:
        _cache_put(key, [])
        return []

    # Top positions only. The tail of a large 13F is index-like and dilutes the
    # measurement of whatever the manager was actually expressing.
    top = sorted(by_symbol.items(), key=lambda kv: -kv[1][0])[:MAX_POSITIONS]
    total = sum(v for _, (v, _sh) in top) or 1.0
    holdings = [Holding(symbol=sym, weight=round(v / total, 6),
                        value_usd=round(v, 2), shares=round(sh, 2))
                for sym, (v, sh) in top]
    _cache_put(key, [asdict(h) for h in holdings])
    return holdings


def _measure(holdings: list[Holding], start: str,
             end: str) -> tuple[float, int, float] | None:
    """
    Value weighted return of a book, with the share of it that could be priced.

    The renormalisation over priced names is still here, because a book with one
    unlisted line in it is still that book. What is new is that the caller is
    told how much of the weight the number rests on, so a window that priced a
    fifth of a portfolio can be thrown out instead of reported as the portfolio.
    """
    total = sum(h.weight for h in holdings)
    if total <= 0:
        return None
    priced = [(h, _ret(h.symbol, start, end)) for h in holdings]
    priced = [(h, r) for h, r in priced if r is not None]
    if not priced:
        return None
    weight = sum(h.weight for h, _ in priced)
    if weight <= 0:
        return None
    return (sum(h.weight * r for h, r in priced) / weight,
            len(priced), weight / total)


def fund_tracker(name: str, cik: str, *, quarters: int = 4) -> Tracker:
    """
    A filer's disclosed book, measured over each of the last few quarters.

    Each window runs from one filing date to the next, so the book being
    measured is the one that was public at the time. The final window runs from
    the most recent filing to today.
    """
    cik = str(cik).zfill(10)
    key = f"fund-{cik}"
    tracker = Tracker(key=key, name=name, kind="fund",
                      source=f"SEC 13F-HR, CIK {int(cik)}")

    filings = sm._filings(cik, "13F-HR", limit=quarters + 1)
    if not filings:
        tracker.error = "no 13F filings found"
        return tracker

    tracker.as_of = filings[0]["filed"]
    tracker.stale_days = (dt.date.today() - dt.date.fromisoformat(filings[0]["filed"])).days

    tracker.holdings = _book(cik, filings[0]["accession"])
    if not tracker.holdings:
        tracker.error = "no priceable US listed positions in the latest filing"
        return tracker

    today = dt.date.today().isoformat()
    # Oldest first, so windows read left to right in time.
    ordered = list(reversed(filings))
    for i, filing in enumerate(ordered):
        start = filing["filed"]
        end = ordered[i + 1]["filed"] if i + 1 < len(ordered) else today
        if start >= end:
            continue
        book = _book(cik, filing["accession"])
        if not book:
            continue
        measured = _measure(book, start, end)
        bench = _ret(BENCH, start, end)
        if measured is None or bench is None:
            continue
        ret, n, coverage = measured
        tracker.windows.append(Window(
            label=f"{start} to {end}", start=start, end=end,
            ret=round(ret, 2), bench=round(bench, 2), names=n,
            coverage=round(coverage, 4)))

    _summarise(tracker)
    tracker.note = (
        f"{len(tracker.holdings)} largest disclosed US long positions, value "
        f"weighted as filed. Excludes shorts, cash, bonds and anything held "
        f"outside the US, so this is the book's return and not the fund's.")
    return tracker


# --------------------------------------------------------------------------
# inverse trackers
# --------------------------------------------------------------------------

# A share count change smaller than this is drift, not a decision.
MATERIAL_CHANGE = 0.10


def _moves(cik: str, newer: str, older: str) -> dict[str, float]:
    """
    Share count change per symbol between two filings, as a fraction.

    Positive is accumulation, negative is a trim. A name absent from the older
    filing is a new position and reads as +1.0; a name absent from the newer
    one was exited and reads as -1.0.
    """
    cur = {h.symbol: h.shares for h in _book(cik, newer)}
    prev = {h.symbol: h.shares for h in _book(cik, older)}
    out: dict[str, float] = {}
    for symbol in set(cur) | set(prev):
        a, b = prev.get(symbol, 0.0), cur.get(symbol, 0.0)
        if a <= 0 and b > 0:
            out[symbol] = 1.0
        elif b <= 0 and a > 0:
            out[symbol] = -1.0
        elif a > 0:
            out[symbol] = (b - a) / a
    return out


def inverse_tracker(name: str, cik: str, *, quarters: int = 4) -> Tracker:
    """
    Do the opposite of what a manager did.

    For a long only account, "inverse" cannot mean shorting their book. It
    means this: buy what they sold. Each quarter the inverse holds, equal
    weighted, the names the manager materially trimmed or exited, and it holds
    nothing they were accumulating.

    That is a real strategy and it is measurable, which is the point. It is
    also not the same thing as betting against them, and the difference
    matters: a manager who exits a winner to take profits will make this look
    clever, and one who exits a loser before it falls further will make it look
    stupid, and neither says anything about whether they were right.
    """
    cik = str(cik).zfill(10)
    tracker = Tracker(key=f"inverse-{cik}", name=f"Inverse {name}", kind="inverse",
                      source=f"SEC 13F-HR, CIK {int(cik)}, positions reversed")

    filings = sm._filings(cik, "13F-HR", limit=quarters + 2)
    if len(filings) < 2:
        tracker.error = "needs at least two filings to see a change"
        return tracker

    tracker.as_of = filings[0]["filed"]
    tracker.stale_days = (dt.date.today() - dt.date.fromisoformat(filings[0]["filed"])).days

    today = dt.date.today().isoformat()
    ordered = list(reversed(filings))       # oldest first

    for i in range(1, len(ordered)):
        newer, older = ordered[i], ordered[i - 1]
        start = newer["filed"]
        end = ordered[i + 1]["filed"] if i + 1 < len(ordered) else today
        if start >= end:
            continue

        moves = _moves(cik, newer["accession"], older["accession"])
        sold = sorted(s for s, change in moves.items() if change <= -MATERIAL_CHANGE)
        if not sold:
            continue

        rets = [(s, _ret(s, start, end)) for s in sold]
        rets = [(s, r) for s, r in rets if r is not None]
        bench = _ret(BENCH, start, end)
        if not rets or bench is None:
            continue

        tracker.windows.append(Window(
            label=f"{start} to {end}, bought what they sold",
            start=start, end=end,
            ret=round(statistics.fmean(r for _, r in rets), 2),
            bench=round(bench, 2), names=len(rets),
            coverage=round(len(rets) / len(sold), 4)))

    if len(ordered) >= 2:
        latest = _moves(cik, ordered[-1]["accession"], ordered[-2]["accession"])
        sold_now = sorted(s for s, change in latest.items() if change <= -MATERIAL_CHANGE)
        tracker.holdings = [Holding(symbol=s, weight=round(1 / len(sold_now), 6))
                            for s in sold_now] if sold_now else []

    _summarise(tracker)
    tracker.note = (
        f"Equal weighted holdings in the names {name} materially trimmed or "
        f"exited, entered on the filing date. Long only, so this is buying what "
        f"they sold rather than shorting what they bought.")
    return tracker


# --------------------------------------------------------------------------
# congressional trackers
# --------------------------------------------------------------------------

def congress_tracker(name: str, *, member: str | None = None,
                     lookback_days: int = 400,
                     hold_days: int = 90) -> Tracker:
    """
    Disclosed congressional purchases, held for a fixed period from the date
    they became public.

    Equal weighted, because the filing gives a dollar range and not an amount.
    Purchases only: a sale tells you someone wanted out of a position whose
    size you cannot see, which is not a strategy anyone can follow.
    """
    key = f"congress-{(member or 'all').lower().replace(' ', '-')}"
    tracker = Tracker(key=key, name=name, kind="congress",
                      source="House Clerk periodic transaction reports")

    try:
        universe = sorted({v["ticker"] for v in cusip_map().values()
                           if v.get("listed") and v.get("ticker")})
        trades = sm.congress_trades(universe, lookback_days=lookback_days,
                                    max_reports=220)
    except Exception as e:  # noqa: BLE001
        tracker.error = f"could not read disclosures: {e}"
        return tracker

    buys = [t for t in trades if t.direction == "BUY"]
    if member:
        needle = member.lower()
        buys = [t for t in buys if needle in t.actor.lower()]
    if not buys:
        tracker.error = ("no disclosed purchases in the window"
                         + (f" for {member}" if member else ""))
        return tracker

    tracker.as_of = max(t.filed_on for t in buys)
    tracker.stale_days = (dt.date.today() - dt.date.fromisoformat(tracker.as_of)).days

    # Each purchase is its own little position, entered the day it became
    # public and held for a fixed window. Grouping by filing month keeps the
    # windows readable without averaging away the entry timing.
    by_month: dict[str, list[Any]] = {}
    for t in buys:
        by_month.setdefault(t.filed_on[:7], []).append(t)

    today = dt.date.today()
    for month in sorted(by_month):
        batch = by_month[month]
        start = min(t.filed_on for t in batch)
        end_date = min(dt.date.fromisoformat(start) + dt.timedelta(days=hold_days), today)
        end = end_date.isoformat()
        if end <= start:
            continue
        symbols = sorted({t.symbol for t in batch})
        rets = [(s, _ret(s, start, end)) for s in symbols]
        rets = [(s, r) for s, r in rets if r is not None]
        bench = _ret(BENCH, start, end)
        if not rets or bench is None:
            continue
        tracker.windows.append(Window(
            label=f"{month} buys, {hold_days}d hold", start=start, end=end,
            ret=round(statistics.fmean(r for _, r in rets), 2),
            bench=round(bench, 2), names=len(rets),
            coverage=round(len(rets) / len(symbols), 4)))

    recent = sorted({t.symbol for t in buys
                     if t.filed_on >= (today - dt.timedelta(days=90)).isoformat()})
    tracker.holdings = [Holding(symbol=s, weight=round(1 / len(recent), 6))
                        for s in recent] if recent else []

    _summarise(tracker)
    tracker.note = (
        f"Equal weighted disclosed purchases, entered on the disclosure date "
        f"and held {hold_days} days. Members file a dollar range rather than "
        f"an amount, so position sizes are unknowable and are not guessed at.")
    return tracker


# --------------------------------------------------------------------------

def _compound(values: Iterable[float]) -> float:
    """Chain period returns given in percent, and hand back percent."""
    total = 1.0
    for v in values:
        total *= 1 + v / 100
    return (total - 1) * 100


def _summarise(tracker: Tracker) -> None:
    """
    Collapse the windows into one number over one span.

    Two things this deliberately does not do. It does not average the window
    excesses, because the windows are not the same length: four quarters and the
    fortnight since the newest filing used to carry one vote each, which handed
    a fifth of every manager's score to the last two weeks of tape. And it does
    not keep a window that priced only a sliver of the book, because
    renormalising over the survivors turns whatever is left into the manager.
    """
    if not tracker.windows:
        tracker.error = tracker.error or "no window could be priced"
        return

    usable = [w for w in tracker.windows if w.measurable]
    tracker.dropped_windows = len(tracker.windows) - len(usable)
    tracker.windows = usable
    if len(usable) < MIN_WINDOWS:
        tracker.error = tracker.error or (
            "%d of %d windows priced %.0f%% of the book across at least %d "
            "names, which is under the %d needed to call it a record"
            % (len(usable), len(usable) + tracker.dropped_windows,
               MIN_COVERAGE * 100, MIN_NAMES, MIN_WINDOWS))
        return

    book = _compound(w.ret for w in usable)
    bench = _compound(w.bench for w in usable)
    tracker.excess = round(book - bench, 2)
    tracker.span_days = sum(w.days for w in usable)
    excesses = [w.excess for w in usable]
    tracker.latest_excess = excesses[-1]
    tracker.beat_rate = round(sum(1 for e in excesses if e > 0) / len(excesses), 3)


def build(*, ttl: float = 12 * 3600) -> dict[str, Any]:
    """
    Every configured tracker, measured and ranked by mean excess over SPY.

    Ranked by the compounded excess over each book's whole measured span rather
    than by the latest window, because one good quarter is noise. The board
    still shows the latest window and the number of windows behind each figure,
    so a record resting on one lucky quarter is visible as one.
    """
    cached = _cache_get("board", ttl)
    if cached is not None:
        return cached

    cfg = config.load("trackers")
    out: list[Tracker] = []

    for spec in cfg.get("funds", []):
        try:
            out.append(fund_tracker(spec["name"], spec["cik"],
                                    quarters=int(cfg.get("quarters", 4))))
        except Exception as e:  # noqa: BLE001
            log.warning("tracker %s failed: %s", spec.get("name"), e)
            out.append(Tracker(key=f"fund-{spec.get('cik')}", name=spec["name"],
                               kind="fund", error=str(e)[:160]))

    for spec in cfg.get("inverse", []):
        try:
            out.append(inverse_tracker(spec["name"], spec["cik"],
                                       quarters=int(cfg.get("quarters", 4))))
        except Exception as e:  # noqa: BLE001
            log.warning("inverse tracker %s failed: %s", spec.get("name"), e)

    for spec in cfg.get("congress", []):
        try:
            out.append(congress_tracker(
                spec["name"], member=spec.get("member"),
                hold_days=int(spec.get("hold_days", 90))))
        except Exception as e:  # noqa: BLE001
            log.warning("tracker %s failed: %s", spec.get("name"), e)

    measured = [t for t in out if t.excess is not None]
    unmeasured = [t for t in out if t.excess is None]
    measured.sort(key=lambda t: t.excess, reverse=True)

    beat = [t for t in measured if t.excess > 0]
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "benchmark": BENCH,
        "trackers": [t.to_dict() for t in measured + unmeasured],
        "summary": {
            "measured": len(measured),
            "beating_benchmark": len(beat),
            "median_excess": (round(statistics.median(
                [t.excess for t in measured]), 2) if measured else None),
            "dropped_windows": sum(t.dropped_windows for t in out),
            "note": (f"{len(beat)} of {len(measured)} measured trackers beat "
                     f"{BENCH}, compounded over each book's own measured span "
                     f"and held from the date each position became public "
                     f"rather than from the trade date."),
        },
    }
    _cache_put("board", payload)
    return payload


def top_holdings(payload: dict[str, Any], n: int = 12) -> list[dict[str, Any]]:
    """
    What the trackers that actually beat the benchmark are holding.

    Restricted to those with positive excess on purpose: a consensus built from
    every filer is just a market cap weighted index with extra steps.
    """
    weights: dict[str, float] = {}
    backers: dict[str, set[str]] = {}
    for t in payload.get("trackers", []):
        if (t.get("excess") or 0) <= 0:
            continue
        # A manager who stopped filing still has a track record but no current
        # book. Scion's last 13F is most of a year old; treating those names as
        # a live view would be reading a stale position as a fresh one.
        if (t.get("stale_days") or 0) > MAX_BOOK_AGE_DAYS:
            continue
        for h in t.get("holdings", []):
            weights[h["symbol"]] = weights.get(h["symbol"], 0.0) + h["weight"]
            backers.setdefault(h["symbol"], set()).add(t["name"])

    rows = [{"symbol": s, "score": round(w, 4),
             "backers": sorted(backers[s]), "n_backers": len(backers[s])}
            for s, w in weights.items()]
    rows.sort(key=lambda r: (r["n_backers"], r["score"]), reverse=True)
    return rows[:n]
