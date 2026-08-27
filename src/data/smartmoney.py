"""
What the people who file with the government are actually buying.

Three independent public sources, none of which needs an API key:

  Congress      House Clerk periodic transaction reports. Members must file
                within 45 days of a trade. This is the "Pelosi tracker" source,
                and it is the primary document rather than somebody's scrape of
                it.

  13F           Quarterly institutional holdings from SEC EDGAR for a curated
                list of funds. Slow (45 day lag) and stale by construction, but
                it is the only look inside a real book you get for free.

  Form 4        Insider transactions from SEC EDGAR, filed within two business
                days. Faster than everything else here, and an officer buying
                their own stock with their own money is the least ambiguous
                signal on this page.

All three are noisy and all three are lagged. They are wired into the rubric as
one small component, not as a trigger. Somebody else's fill is not a thesis.

Everything is cached to disk with a TTL because these are public services with
rate limits and a courtesy contact header, and hammering them would be both
rude and slow.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, field, asdict
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import requests

from .. import config
from ..config import DATA_DIR

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "smartmoney"

# The SEC asks every automated client to identify itself with a real contact
# address, and throttles to about ten requests a second. Both are conditions of
# use rather than suggestions.
#
# This is read from the environment and has no default on purpose. A hardcoded
# address means every copy of this code identifies as one person, which is both
# a privacy leak for them and a false declaration to the SEC by everyone else.
# With it unset the SEC sources are skipped and say why.
SEC_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
SEC_UA = f"portfolio-manager {SEC_CONTACT}" if SEC_CONTACT else ""
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}
SEC_MIN_INTERVAL = 0.12

HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

_last_sec_call = 0.0


# --------------------------------------------------------------------------
# types
# --------------------------------------------------------------------------

@dataclass
class Trade:
    """One disclosed transaction, from any of the three sources."""
    source: str            # congress | insider | fund
    actor: str             # member name, insider name, or fund name
    symbol: str
    direction: str         # BUY | SELL
    #: midpoint of the disclosed range in dollars, or the actual value where
    #: the filing gives one. Congress files ranges, so this is an estimate and
    #: is labelled as such everywhere it is shown.
    value_usd: float
    traded_on: str         # ISO date of the transaction
    filed_on: str          # ISO date it became public
    detail: str = ""

    @property
    def lag_days(self) -> int:
        try:
            a = dt.date.fromisoformat(self.traded_on)
            b = dt.date.fromisoformat(self.filed_on)
            return max((b - a).days, 0)
        except ValueError:
            return 0


@dataclass
class SmartMoneySignal:
    """The per-symbol rollup that reaches the rubric."""
    symbol: str
    score: float | None            # 0-100, None when nothing was disclosed
    buyers: int = 0
    sellers: int = 0
    net_usd: float = 0.0
    congress_net: float = 0.0
    insider_net: float = 0.0
    fund_net: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trades"] = [asdict(t) | {"lag_days": t.lag_days}
                       for t in self.trades[:40]]
        return d


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key).replace("..", "_")
    return CACHE_DIR / f"{safe}.json"


def _cache_get(key: str, ttl: float) -> Any | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cache_put(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(key).write_text(json.dumps(value, default=str),
                                    encoding="utf-8")
    except OSError as e:
        log.warning("could not cache %s: %s", key, e)


class SECContactMissing(RuntimeError):
    """Raised rather than sending an anonymous or borrowed identity."""


def _sec_get(url: str, *, timeout: float = 30.0) -> requests.Response:
    """Every SEC call goes through here so the rate limit is honoured once."""
    global _last_sec_call
    if not SEC_CONTACT:
        raise SECContactMissing(
            "SEC_CONTACT_EMAIL is not set. The SEC requires automated clients "
            "to identify themselves with a real contact address, so insider "
            "filings and 13F data are unavailable until you set it. Put your "
            "own email in .env; it is sent to the SEC and nowhere else.")
    wait = SEC_MIN_INTERVAL - (time.time() - _last_sec_call)
    if wait > 0:
        time.sleep(wait)
    _last_sec_call = time.time()
    return requests.get(url, headers=SEC_HEADERS, timeout=timeout)


# --------------------------------------------------------------------------
# congress
# --------------------------------------------------------------------------

# One disclosed line looks like this once the PDF text is flattened:
#
#   Alphabet Inc. - Class A Common Stock (GOOGL) [ST] P 07/17/202608/06/2026
#   $1,001 - $15,000
#
# The two dates run together because the PDF has them in adjacent cells with no
# separating space, which is why the date groups are matched without a gap.
_PTR_ROW = re.compile(
    r"\((?P<ticker>[A-Z][A-Z.\-]{0,5})\)\s*"
    r"\[(?P<kind>ST|OP|OT|ET|MF|CS)\]\s*"
    r"(?P<action>P|S \(partial\)|S|E)\s*"
    r"(?P<traded>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<filed>\d{2}/\d{2}/\d{4})\s*"
    r"\$(?P<low>[\d,]+)\s*-\s*\$(?P<high>[\d,]+)"
)


def _to_iso(us_date: str) -> str:
    try:
        return dt.datetime.strptime(us_date, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def _money(text: str) -> float:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


def house_index(year: int, *, ttl: float = 6 * 3600) -> list[dict[str, str]]:
    """
    The year's filing index. Returns only periodic transaction reports, which
    are the ones that name a security. Annual disclosures are a different form
    and are not trades.
    """
    key = f"house_index_{year}"
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached

    try:
        resp = requests.get(HOUSE_INDEX.format(year=year),
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
        resp.raise_for_status()
        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            text = zf.read(name).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - a dead source must not kill the run
        log.warning("house index %s unavailable: %s", year, e)
        return []

    rows: list[dict[str, str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 9 or parts[4].strip() != "P":
            continue
        rows.append({
            "last": parts[1].strip(), "first": parts[2].strip(),
            "state": parts[5].strip(), "filed": _to_iso(parts[7].strip()),
            "doc": parts[8].strip(), "year": str(year),
        })
    _cache_put(key, rows)
    return rows


def _parse_ptr(doc_id: str, year: str, member: str, filed: str) -> list[Trade]:
    """
    One member's report. Parsed reports are cached forever because a filed
    disclosure never changes.
    """
    key = f"ptr_{year}_{doc_id}"
    cached = _cache_get(key, ttl=365 * 24 * 3600)
    if cached is not None:
        return [Trade(**t) for t in cached]

    trades: list[Trade] = []
    try:
        resp = requests.get(HOUSE_PTR.format(year=year, doc=doc_id),
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
        resp.raise_for_status()
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(resp.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001
        log.debug("PTR %s unreadable: %s", doc_id, e)
        _cache_put(key, [])
        return []

    # Filings before electronic submission are scans with no text layer. There
    # is nothing to parse and no point retrying, so they cache as empty.
    flat = re.sub(r"\s+", " ", text)
    for m in _PTR_ROW.finditer(flat):
        low, high = _money(m.group("low")), _money(m.group("high"))
        trades.append(Trade(
            source="congress",
            actor=member,
            symbol=m.group("ticker").upper(),
            direction="SELL" if m.group("action").startswith("S") else "BUY",
            value_usd=(low + high) / 2.0,
            traded_on=_to_iso(m.group("traded")),
            filed_on=_to_iso(m.group("filed")) or filed,
            detail=f"{m.group('kind')} disclosed range ${low:,.0f} to ${high:,.0f}",
        ))

    _cache_put(key, [asdict(t) for t in trades])
    return trades


def congress_trades(symbols: Iterable[str], *, lookback_days: int = 120,
                    max_reports: int = 60) -> list[Trade]:
    """
    Recent disclosed congressional trades in our symbols.

    `max_reports` bounds the work per run. Reports are read newest first, so a
    cold cache fills in from the most recent filings rather than from January.
    """
    wanted = {s.upper() for s in symbols}
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)

    rows = house_index(today.year)
    if today.month <= 3:
        rows += house_index(today.year - 1)

    recent = [r for r in rows if r["filed"] and r["filed"] >= cutoff.isoformat()]
    recent.sort(key=lambda r: r["filed"], reverse=True)

    out: list[Trade] = []
    for row in recent[:max_reports]:
        member = f"{row['first']} {row['last']}".strip()
        for trade in _parse_ptr(row["doc"], row["year"], member, row["filed"]):
            if trade.symbol in wanted and trade.traded_on >= cutoff.isoformat():
                out.append(trade)
    return out


# --------------------------------------------------------------------------
# 13F
# --------------------------------------------------------------------------

def _ticker_map(*, ttl: float = 30 * 24 * 3600) -> dict[str, str]:
    """Company name to ticker, from the SEC's own list. Used to read 13Fs."""
    cached = _cache_get("sec_tickers", ttl)
    if cached is None:
        try:
            resp = _sec_get("https://www.sec.gov/files/company_tickers.json")
            resp.raise_for_status()
            cached = resp.json()
            _cache_put("sec_tickers", cached)
        except Exception as e:  # noqa: BLE001
            log.warning("SEC ticker list unavailable: %s", e)
            return {}
    out: dict[str, str] = {}
    for row in (cached or {}).values():
        if isinstance(row, dict) and row.get("title") and row.get("ticker"):
            out[_norm_name(row["title"])] = str(row["ticker"]).upper()
    return out


def cik_for(symbol: str) -> str | None:
    cached = _cache_get("sec_tickers", 30 * 24 * 3600)
    if cached is None:
        _ticker_map()
        cached = _cache_get("sec_tickers", 30 * 24 * 3600) or {}
    for row in (cached or {}).values():
        if isinstance(row, dict) and str(row.get("ticker", "")).upper() == symbol.upper():
            return str(row["cik_str"]).zfill(10)
    return None


_NAME_NOISE = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|plc|the|class|cl|com|common|"
    r"stock|holdings|holding|group|sa|nv|ag|lp|trust|new|del)\b\.?")


def _norm_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = _NAME_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _filings(cik: str, form: str, limit: int) -> list[dict[str, str]]:
    key = f"subs_{cik}_{form}"
    cached = _cache_get(key, ttl=12 * 3600)
    if cached is not None:
        return cached[:limit]
    try:
        resp = _sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
    except Exception as e:  # noqa: BLE001
        log.warning("submissions for %s unavailable: %s", cik, e)
        return []
    rows = [
        {"form": recent["form"][i],
         "filed": recent["filingDate"][i],
         "accession": recent["accessionNumber"][i].replace("-", "")}
        for i in range(len(recent.get("form", [])))
        if recent["form"][i].startswith(form)
    ]
    _cache_put(key, rows)
    return rows[:limit]


def _info_table(cik: str, accession: str) -> dict[str, float]:
    """Issuer name to dollar value, aggregated across managers in one filing."""
    key = f"13f_{cik}_{accession}"
    cached = _cache_get(key, ttl=365 * 24 * 3600)
    if cached is not None:
        return cached

    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    try:
        idx = _sec_get(f"{base}/index.json")
        idx.raise_for_status()
        names = [i["name"] for i in idx.json()["directory"]["item"]]
        table = next((n for n in names
                      if n.endswith(".xml") and "primary_doc" not in n), None)
        if not table:
            _cache_put(key, {})
            return {}
        doc = _sec_get(f"{base}/{table}")
        doc.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(doc.content)
    except Exception as e:  # noqa: BLE001
        log.warning("13F %s/%s unreadable: %s", cik, accession, e)
        return {}

    holdings: dict[str, float] = {}
    for node in root.iter():
        if not node.tag.endswith("infoTable"):
            continue
        issuer = value = None
        for child in node.iter():
            if child.tag.endswith("nameOfIssuer"):
                issuer = child.text
            elif child.tag.endswith("value"):
                value = child.text
        if issuer and value:
            # One issuer appears once per manager with discretion over it, so
            # the rows have to be summed rather than taken.
            try:
                holdings[issuer.strip()] = holdings.get(issuer.strip(), 0.0) + float(value)
            except ValueError:
                continue

    _cache_put(key, holdings)
    return holdings


def fund_moves(symbols: Iterable[str]) -> list[Trade]:
    """
    Quarter-on-quarter position changes for the tracked funds, restricted to
    our symbols.

    Values reported in 13F are dollar values at quarter end, so a position that
    did not change in share count still moves in value with the price. Only
    changes past a threshold are treated as a decision.
    """
    wanted = {s.upper() for s in symbols}
    names = _ticker_map()
    cfg = config.load("smartmoney")
    threshold = float(cfg.get("fund_change_threshold", 0.20))
    stale_after = int(cfg.get("max_staleness_days", 200))
    oldest_ok = (dt.date.today() - dt.timedelta(days=stale_after)).isoformat()

    out: list[Trade] = []
    for fund in cfg.get("funds", []):
        cik = str(fund["cik"]).zfill(10)
        filings = _filings(cik, "13F-HR", limit=2)
        if len(filings) < 2:
            continue
        # A manager who stopped filing is a dead source, not a silent one.
        # Reporting their last book as current would age into a lie.
        if filings[0]["filed"] < oldest_ok:
            log.info("skipping %s, newest 13F is %s",
                     fund["name"], filings[0]["filed"])
            continue
        now = _info_table(cik, filings[0]["accession"])
        prev = _info_table(cik, filings[1]["accession"])
        if not now:
            continue

        for issuer, value in now.items():
            ticker = names.get(_norm_name(issuer))
            if not ticker or ticker not in wanted:
                continue
            before = prev.get(issuer, 0.0)
            if before <= 0:
                direction, detail = "BUY", "new position"
            else:
                change = (value - before) / before
                if abs(change) < threshold:
                    continue
                direction = "BUY" if change > 0 else "SELL"
                detail = f"position {'up' if change > 0 else 'down'} {abs(change) * 100:.0f}% on the quarter"
            out.append(Trade(
                source="fund", actor=fund["name"], symbol=ticker,
                direction=direction, value_usd=abs(value - before),
                traded_on=filings[0]["filed"], filed_on=filings[0]["filed"],
                detail=detail,
            ))

        for issuer, before in prev.items():
            ticker = names.get(_norm_name(issuer))
            if ticker and ticker in wanted and issuer not in now:
                out.append(Trade(
                    source="fund", actor=fund["name"], symbol=ticker,
                    direction="SELL", value_usd=before,
                    traded_on=filings[0]["filed"], filed_on=filings[0]["filed"],
                    detail="position closed",
                ))
    return out


# --------------------------------------------------------------------------
# Form 4
# --------------------------------------------------------------------------

def insider_trades(symbols: Iterable[str], *, lookback_days: int = 120,
                   max_per_symbol: int = 12) -> list[Trade]:
    """
    Open market insider buys and sells. Transaction code P is an open market
    purchase and S is an open market sale; everything else (option exercises,
    grants, gifts, tax withholding) is compensation machinery rather than an
    opinion, and is skipped.
    """
    cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    out: list[Trade] = []

    for symbol in symbols:
        cik = cik_for(symbol)
        if not cik:
            continue
        for filing in _filings(cik, "4", limit=max_per_symbol):
            if filing["filed"] < cutoff:
                break
            out.extend(_parse_form4(cik, filing["accession"], symbol,
                                    filing["filed"]))
    return out


def _parse_form4(cik: str, accession: str, symbol: str,
                 filed: str) -> list[Trade]:
    key = f"form4_{cik}_{accession}"
    cached = _cache_get(key, ttl=365 * 24 * 3600)
    if cached is not None:
        return [Trade(**t) for t in cached]

    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    trades: list[Trade] = []
    try:
        idx = _sec_get(f"{base}/index.json")
        idx.raise_for_status()
        name = next((i["name"] for i in idx.json()["directory"]["item"]
                     if i["name"].endswith(".xml")
                     and not i["name"].startswith("0")), None)
        if not name:
            _cache_put(key, [])
            return []
        doc = _sec_get(f"{base}/{name}")
        doc.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(doc.content)
    except Exception as e:  # noqa: BLE001
        log.debug("form 4 %s unreadable: %s", accession, e)
        _cache_put(key, [])
        return []

    def text_at(node: Any, *path: str) -> str:
        cur = node
        for step in path:
            cur = cur.find(step) if cur is not None else None
        if cur is None:
            return ""
        value = cur.find("value")
        return ((value.text if value is not None else cur.text) or "").strip()

    # Trust the filing over our own CIK lookup. If the document says it is
    # about a different issuer than the one we asked for, the mapping is wrong
    # and attributing these trades to `symbol` would be a quiet fabrication.
    filed_symbol = text_at(root, "issuer", "issuerTradingSymbol").upper()
    if filed_symbol and filed_symbol != symbol.upper():
        log.warning("form 4 %s is for %s, not %s: skipping",
                    accession, filed_symbol, symbol)
        _cache_put(key, [])
        return []

    owner = text_at(root, "reportingOwner", "reportingOwnerId",
                    "rptOwnerName") or "insider"
    title = text_at(root, "reportingOwner", "reportingOwnerRelationship",
                    "officerTitle")

    for txn in root.iter("nonDerivativeTransaction"):
        code = text_at(txn, "transactionCoding", "transactionCode")
        if code not in ("P", "S"):
            continue
        try:
            shares = float(text_at(txn, "transactionAmounts", "transactionShares"))
            price = float(text_at(txn, "transactionAmounts", "transactionPricePerShare"))
        except ValueError:
            continue
        trades.append(Trade(
            source="insider", actor=owner.title(), symbol=symbol.upper(),
            direction="BUY" if code == "P" else "SELL",
            value_usd=shares * price,
            traded_on=text_at(txn, "transactionDate") or filed,
            filed_on=filed,
            detail=f"{title or 'insider'}, {shares:,.0f} shares at ${price:,.2f}",
        ))

    _cache_put(key, [asdict(t) for t in trades])
    return trades


# --------------------------------------------------------------------------
# rollup
# --------------------------------------------------------------------------

def collect(symbols: Iterable[str] | None = None,
            *, ttl: float = 6 * 3600) -> dict[str, SmartMoneySignal]:
    """
    Every source, rolled up per symbol and scored 0-100.

    A symbol nobody disclosed a trade in scores None rather than 50. Silence is
    not neutrality, it is absence of data, and the rubric handles the two
    differently: a None here leaves the component UNSCORED and the remaining
    weights are renormalised over what was actually measured.
    """
    # Active set by default, not the whole universe. Form 4 is several SEC
    # requests per symbol, so defaulting to every screened name would turn
    # one dashboard load into a thousand requests.
    syms = [s.upper() for s in (symbols or config.active_symbols()) if "/" not in s]
    key = "rollup_" + "_".join(sorted(syms))[:120]
    cached = _cache_get(key, ttl)
    if cached is not None:
        return {k: _signal_from_dict(v) for k, v in cached.items()}

    cfg = config.load("smartmoney")
    trades: list[Trade] = []
    for name, fn in (("congress", congress_trades),
                     ("fund", fund_moves),
                     ("insider", insider_trades)):
        if not cfg.get("sources", {}).get(name, True):
            continue
        try:
            trades.extend(fn(syms))
        except Exception as e:  # noqa: BLE001 - one dead source is not fatal
            log.warning("smart money source %s failed: %s", name, e)

    signals: dict[str, SmartMoneySignal] = {}
    for symbol in syms:
        mine = [t for t in trades if t.symbol == symbol]
        signals[symbol] = _score_symbol(symbol, mine, cfg)

    _cache_put(key, {k: v.to_dict() for k, v in signals.items()})
    return signals


def _score_symbol(symbol: str, trades: list[Trade],
                  cfg: dict[str, Any]) -> SmartMoneySignal:
    if not trades:
        return SmartMoneySignal(symbol=symbol, score=None,
                                note="nothing disclosed in the window")

    weights = cfg.get("source_weights", {})
    half_life = float(cfg.get("decay_half_life_days", 45))
    today = dt.date.today()

    weighted = 0.0
    gross = 0.0
    decay_sum = 0.0
    per_source = {"congress": 0.0, "insider": 0.0, "fund": 0.0}

    for t in trades:
        try:
            age = (today - dt.date.fromisoformat(t.traded_on)).days
        except ValueError:
            age = int(half_life)
        # A disclosure about a trade from four months ago says less about today
        # than one from last week, so weight decays with the age of the trade
        # rather than the age of the filing.
        decay = 0.5 ** (max(age, 0) / half_life)
        w = float(weights.get(t.source, 1.0)) * decay
        signed = t.value_usd * (1 if t.direction == "BUY" else -1)
        weighted += signed * w
        gross += t.value_usd * w
        decay_sum += decay
        per_source[t.source] = per_source.get(t.source, 0.0) + signed

    # Net over gross keeps a symbol with one big buy from outranking a symbol
    # with ten. The result is a lopsidedness measure in [-1, 1], not a size one.
    tilt = (weighted / gross) if gross > 0 else 0.0

    # Decay cancels out of `tilt`, because it scales the numerator and the
    # denominator alike: six disclosures all a year old are just as lopsided
    # as six from last week. So age has to enter through conviction instead,
    # otherwise a stale unanimous signal scores exactly like a fresh one.
    freshness = (decay_sum / len(trades)) if trades else 0.0
    depth = min(len(trades) / float(cfg.get("full_conviction_trades", 6)), 1.0)
    conviction = depth * freshness
    score = 50.0 + (tilt * conviction * 50.0)

    buyers = sum(1 for t in trades if t.direction == "BUY")
    sellers = len(trades) - buyers
    actors = ", ".join(sorted({t.actor for t in trades})[:3])

    return SmartMoneySignal(
        symbol=symbol,
        score=round(max(0.0, min(100.0, score)), 2),
        buyers=buyers, sellers=sellers,
        net_usd=round(sum(t.value_usd * (1 if t.direction == "BUY" else -1)
                          for t in trades), 2),
        congress_net=round(per_source.get("congress", 0.0), 2),
        insider_net=round(per_source.get("insider", 0.0), 2),
        fund_net=round(per_source.get("fund", 0.0), 2),
        trades=sorted(trades, key=lambda t: t.filed_on, reverse=True),
        # The count and the score can point opposite ways, and that is not a
        # bug: the score is weighted by dollars and by source, so one large
        # fund purchase outweighs a dozen small routine insider sales. Say so
        # here, because "85" next to "6 buys against 19 sells" otherwise reads
        # as a contradiction.
        note=(f"{len(trades)} disclosures, {buyers} buys against {sellers} sells "
              f"({actors}). Score is weighted by dollars and source, not by count."),
    )


def _signal_from_dict(d: dict[str, Any]) -> SmartMoneySignal:
    payload = dict(d)
    payload["trades"] = [
        Trade(**{k: v for k, v in t.items() if k != "lag_days"})
        for t in payload.get("trades", [])
    ]
    return SmartMoneySignal(**payload)


def smart_money_score(symbol: str,
                      signals: dict[str, SmartMoneySignal] | None) -> float | None:
    """The rubric's accessor. None means UNSCORED, which the caller respects."""
    if not signals:
        return None
    sig = signals.get(symbol.upper())
    return sig.score if sig else None


def recent_feed(signals: dict[str, SmartMoneySignal], limit: int = 40) -> list[dict[str, Any]]:
    """Newest disclosures across every symbol, for the live rail."""
    rows: list[dict[str, Any]] = []
    for sig in signals.values():
        for t in sig.trades:
            rows.append(asdict(t) | {"lag_days": t.lag_days})
    rows.sort(key=lambda r: (r["filed_on"], r["value_usd"]), reverse=True)
    return rows[:limit]
