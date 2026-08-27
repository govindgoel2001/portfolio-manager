"""
Real-time news over a WebSocket.

The polling watcher checked every twenty minutes, which means a settlement
announced at 09:01 was acted on at 09:20 at the earliest. Alpaca pushes news
items the moment they land, so this closes that window to seconds.

What this process does and deliberately does not do:

  It receives, it prefilters in memory, and it debounces. The prefilter is the
  same cheap keyword and magnitude pass the poller used, so a flood of routine
  headlines costs nothing.

  It does not classify, size or trade. When something material arrives it
  shells out to run_event.py for the affected symbols, which owns the whole
  decision path: Claude classification, Kelly sizing, the deterministic risk
  gate, and execution or queueing. One decision path, not two, so a guardrail
  cannot be enforced on one route and missing on the other.

  Headlines are batched for a few seconds before firing. A single story often
  arrives as three items from three outlets within a second, and each should
  not start its own assessment.

Connection state is written to data/stream.json so the dashboard can show
whether this is actually live rather than assuming it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from . import config, events
from .config import DATA_DIR
from .data.news import NewsItem, _stable_id, _parse_ts

log = logging.getLogger(__name__)

NEWS_WS = os.environ.get(
    "PM_NEWS_WS", "wss://stream.data.alpaca.markets/v1beta1/news")
STATUS_PATH = DATA_DIR / "stream.json"

DEBOUNCE_SECONDS = float(os.environ.get("PM_STREAM_DEBOUNCE", "8"))
MAX_BACKOFF = 300.0
STALE_AFTER_SECONDS = 900.0   # no traffic at all for 15 minutes is suspicious


@dataclass
class StreamStatus:
    connected: bool = False
    state: str = "starting"
    endpoint: str = NEWS_WS
    connected_at: str | None = None
    last_message_at: str | None = None
    messages_seen: int = 0
    material_seen: int = 0
    assessments_fired: int = 0
    subscribed_symbols: int = 0
    reconnects: int = 0
    last_error: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)

    def write(self) -> None:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["written_at"] = _now()
        STATUS_PATH.write_text(json.dumps(payload, indent=2, default=str),
                               encoding="utf-8")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_status() -> dict[str, Any]:
    """Used by the API so the dashboard can show real connection state."""
    if not STATUS_PATH.exists():
        return {"connected": False, "state": "not running",
                "note": "the stream process has never written a status file"}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"connected": False, "state": "unreadable status file"}

    # A status file left behind by a dead process would otherwise read as
    # connected forever.
    written = data.get("written_at")
    if written:
        try:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(written)).total_seconds()
            data["status_age_seconds"] = round(age, 1)
            if age > 120 and data.get("connected"):
                data["connected"] = False
                data["state"] = f"stale, no heartbeat for {age:.0f}s"
        except ValueError:
            pass
    return data


class NewsStream:
    def __init__(self, symbols: list[str] | None = None) -> None:
        self.key = os.environ.get("ALPACA_KEY_ID", "")
        self.secret = os.environ.get("ALPACA_SECRET_KEY", "")
        # The news feed tags equity tickers. Crypto pairs are not carried
        # there, so subscribing to them would silently match nothing.
        self.symbols = [s.upper() for s in (symbols or config.active_symbols())
                        if "/" not in s]
        self.status = StreamStatus(subscribed_symbols=len(self.symbols))
        self._pending: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._seen: set[str] = set()

    # -- the loop ---------------------------------------------------------

    async def run(self) -> None:
        if not (self.key and self.secret):
            self.status.state = "no Alpaca credentials, stream disabled"
            self.status.last_error = (
                "ALPACA_KEY_ID and ALPACA_SECRET_KEY are not set. The news "
                "WebSocket is an Alpaca endpoint, so it cannot connect without "
                "them. Scheduled runs still happen on Hermes's timers, and "
                "run_event.py can still be triggered by hand, but nothing is "
                "watching for breaking news until these keys exist.")
            self.status.write()
            log.warning("%s", self.status.last_error)
            # Keep the process alive and keep the status fresh, so the
            # dashboard shows a deliberate "disabled" rather than "dead".
            while True:
                self.status.write()
                await asyncio.sleep(60)

        try:
            import websockets
        except ImportError:
            self.status.state = "websockets package not installed"
            self.status.write()
            log.error("websockets is not installed")
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(NEWS_WS, ping_interval=20,
                                              ping_timeout=20,
                                              close_timeout=10) as ws:
                    await self._handshake(ws)
                    backoff = 1.0
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure must reconnect
                self.status.connected = False
                self.status.state = "reconnecting"
                self.status.last_error = f"{type(e).__name__}: {e}"
                self.status.reconnects += 1
                self.status.write()
                log.warning("stream dropped (%s), reconnecting in %.0fs",
                            self.status.last_error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _handshake(self, ws: Any) -> None:
        # Alpaca greets, then expects auth, then a subscribe.
        greeting = json.loads(await ws.recv())
        log.debug("greeting: %s", greeting)

        await ws.send(json.dumps({"action": "auth", "key": self.key,
                                  "secret": self.secret}))
        reply = json.loads(await ws.recv())
        if not _has(reply, "T", "success"):
            raise RuntimeError(f"authentication refused: {reply}")

        await ws.send(json.dumps({"action": "subscribe", "news": self.symbols}))
        confirm = json.loads(await ws.recv())
        if not _has(confirm, "T", "subscription"):
            raise RuntimeError(f"subscription refused: {confirm}")

        self.status.connected = True
        self.status.state = "connected"
        self.status.connected_at = _now()
        self.status.last_error = ""
        self.status.write()
        log.info("news stream connected, subscribed to %d symbols",
                 len(self.symbols))

    async def _consume(self, ws: Any) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=STALE_AFTER_SECONDS)
            except asyncio.TimeoutError:
                # Alpaca's own ping keeps the socket open, so total silence for
                # this long means the subscription is not delivering. Bounce it
                # rather than sit on a connection that looks healthy.
                raise RuntimeError(
                    f"no traffic for {STALE_AFTER_SECONDS:.0f}s, cycling the connection")

            self.status.last_message_at = _now()
            for message in _as_list(json.loads(raw)):
                await self._handle(message)
            self.status.write()

    # -- one message ------------------------------------------------------

    async def _handle(self, message: dict[str, Any]) -> None:
        if message.get("T") != "n":
            return
        self.status.messages_seen += 1

        item = _to_news_item(message)
        if item is None or item.id in self._seen:
            return
        self._seen.add(item.id)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2500:])

        score = events.prefilter_score(item)
        entry = {
            "at": _now(), "symbol": item.symbol,
            "headline": item.headline[:160], "score": score,
            "material": score >= events.PREFILTER_THRESHOLD,
        }
        self.status.recent.insert(0, entry)
        del self.status.recent[25:]

        if score < events.PREFILTER_THRESHOLD:
            log.debug("%.2f %s %s", score, item.symbol, item.headline[:70])
            return

        self.status.material_seen += 1
        log.info("MATERIAL %.2f %-8s %s", score, item.symbol, item.headline[:80])

        universe = {s.upper() for s in config.all_symbols()}
        for symbol in _symbols_of(message):
            if symbol in universe:
                self._pending.add(symbol)

        if self._pending and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.create_task(self._flush_after_debounce())

    async def _flush_after_debounce(self) -> None:
        """
        One story usually arrives as several items within a second. Wait a
        moment, then assess the whole batch once.
        """
        await asyncio.sleep(DEBOUNCE_SECONDS)
        symbols = sorted(self._pending)
        self._pending.clear()
        if not symbols:
            return

        log.info("firing assessment for %s", ", ".join(symbols))
        self.status.assessments_fired += 1
        self.status.write()

        root = Path(__file__).resolve().parent.parent
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(root / "run_event.py"),
                "--symbols", ",".join(symbols), "--since", "2h",
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            log.error("assessment for %s timed out", symbols)
            return
        except Exception as e:  # noqa: BLE001
            log.error("assessment for %s failed to start: %s", symbols, e)
            return

        for line in (out or b"").decode(errors="replace").strip().splitlines()[-15:]:
            log.info("  %s", line)
        if proc.returncode != 0:
            log.error("assessment exited %s", proc.returncode)


# --------------------------------------------------------------------------

def _has(message: Any, key: str, value: str) -> bool:
    for entry in _as_list(message):
        if isinstance(entry, dict) and entry.get(key) == value:
            return True
    return False


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return [payload] if isinstance(payload, dict) else []


def _symbols_of(message: dict[str, Any]) -> list[str]:
    return [str(s).upper() for s in (message.get("symbols") or [])]


def _to_news_item(message: dict[str, Any]) -> NewsItem | None:
    headline = message.get("headline") or ""
    if not headline:
        return None
    symbols = _symbols_of(message)
    universe = {s.upper() for s in config.all_symbols()}
    # Attribute to a symbol we actually follow, so the prefilter and any
    # downstream assessment are about a position rather than a bystander.
    symbol = next((s for s in symbols if s in universe), symbols[0] if symbols else "?")
    url = message.get("url", "")
    return NewsItem(
        id=str(message.get("id") or _stable_id(url, headline)),
        symbol=symbol,
        headline=headline,
        summary=(message.get("summary") or "")[:600],
        source=message.get("source", "alpaca"),
        url=url,
        published_at=_parse_ts(message.get("created_at") or _now()),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s stream  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from dotenv import load_dotenv
    load_dotenv()

    stream = NewsStream()
    try:
        asyncio.run(stream.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
