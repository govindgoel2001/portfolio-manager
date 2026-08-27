"""
Persistence: run records, the approval queue, the execution log, and the
per-symbol thesis memory that lets each run know what the last run believed.

Holdings are never stored - the broker is the source of truth for those, and
duplicating them is how a dashboard ends up lying. What is stored here is the
reasoning: what we thought, when, why, and what happened next.

Layout:
    data/runs/<run_id>.json     one frozen record per daily run
    data/theses.json            per-symbol thesis memory across runs
    data/executions.jsonl       append-only log of every order ever sent
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"
THESES_PATH = DATA_DIR / "theses.json"
EXECUTIONS_PATH = DATA_DIR / "executions.jsonl"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
FAILED = "failed"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: str) -> None:
    """Never leave a half-written run record on disk if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DATA_DIR
        self.runs_dir = self.root / "runs"
        self.theses_path = self.root / "theses.json"
        self.executions_path = self.root / "executions.jsonl"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- runs ----------------

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def save_run(self, run: dict[str, Any]) -> Path:
        run.setdefault("saved_at", _now())
        path = self.run_path(run["run_id"])
        _atomic_write(path, json.dumps(run, indent=2, default=str))
        return path

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = self.run_path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 60) -> list[str]:
        ids = sorted((p.stem for p in self.runs_dir.glob("*.json")), reverse=True)
        return ids[:limit]

    def latest_run(self) -> dict[str, Any] | None:
        ids = self.list_runs(1)
        return self.get_run(ids[0]) if ids else None

    # ---------------- approval queue ----------------

    def set_proposal_status(
        self, run_id: str, proposal_id: str, status: str,
        *, note: str = "", order: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"no run {run_id}")
        for p in run.get("proposals", []):
            if p.get("id") == proposal_id:
                p["status"] = status
                p["decided_at"] = _now()
                if note:
                    p["decision_note"] = note
                if order is not None:
                    p["order"] = order
                self.save_run(run)
                return p
        raise KeyError(f"no proposal {proposal_id} in run {run_id}")

    def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        run = self.get_run(run_id) if run_id else self.latest_run()
        if not run:
            return []
        return [p for p in run.get("proposals", []) if p.get("status") == PENDING]

    # ---------------- committee verdicts ----------------

    def committee_path(self, symbol: str) -> Path:
        return self.root / "committee" / f"{symbol.upper()}.json"

    def save_committee(self, symbol: str, verdict: dict[str, Any]) -> None:
        path = self.committee_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        verdict = dict(verdict)
        verdict["at"] = _now()
        _atomic_write(path, json.dumps(verdict, indent=2, default=str))

    def committee(self, symbol: str) -> dict[str, Any] | None:
        path = self.committee_path(symbol)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def all_committees(self) -> dict[str, dict[str, Any]]:
        folder = self.root / "committee"
        if not folder.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for path in folder.glob("*.json"):
            try:
                out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
        return out

    # ---------------- thesis memory ----------------

    def theses(self) -> dict[str, Any]:
        if not self.theses_path.exists():
            return {}
        try:
            return json.loads(self.theses_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def remember_thesis(self, symbol: str, entry: dict[str, Any]) -> None:
        data = self.theses()
        prior = data.get(symbol.upper(), {})
        history = prior.get("history", [])
        if prior.get("thesis") and prior.get("thesis") != entry.get("thesis"):
            history.insert(0, {"as_of": prior.get("updated_at"), "thesis": prior["thesis"]})
            del history[10:]
        data[symbol.upper()] = {**entry, "updated_at": _now(), "history": history}
        _atomic_write(self.theses_path, json.dumps(data, indent=2, default=str))

    def prior_thesis(self, symbol: str) -> dict[str, Any]:
        return self.theses().get(symbol.upper(), {})

    # ---------------- execution log ----------------

    def log_execution(self, record: dict[str, Any]) -> None:
        record.setdefault("at", _now())
        self.executions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.executions_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def executions(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.executions_path.exists():
            return []
        lines = self.executions_path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out

    # ---------------- snapshots ----------------

    def save_snapshot(self, snapshot_id: str, payload: dict[str, Any]) -> Path:
        path = self.root / "snapshots" / f"{snapshot_id}.json"
        _atomic_write(path, json.dumps(payload, indent=2, default=str))
        return path

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self.root / "snapshots" / f"{snapshot_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def iter_snapshots(self) -> Iterator[str]:
        yield from sorted(p.stem for p in (self.root / "snapshots").glob("*.json"))


    # ---------------- event ledger ----------------

    @property
    def _events_path(self):
        return self.root / "events.json"

    def _events(self) -> dict[str, Any]:
        if not self._events_path.exists():
            return {"seen": [], "last_check": None, "autopilot": False,
                    "auto_trades": [], "cooldowns": {}}
        try:
            return json.loads(self._events_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"seen": [], "last_check": None, "autopilot": False,
                    "auto_trades": [], "cooldowns": {}}

    def _save_events(self, data: dict[str, Any]) -> None:
        _atomic_write(self._events_path, json.dumps(data, indent=2, default=str))

    def seen_news(self) -> set[str]:
        return set(self._events().get("seen", []))

    def mark_seen(self, ids: list[str]) -> None:
        data = self._events()
        merged = list(dict.fromkeys(list(ids) + data.get("seen", [])))
        data["seen"] = merged[:5000]   # bounded, newest first
        self._save_events(data)

    def last_event_check(self) -> dt.datetime | None:
        raw = self._events().get("last_check")
        return dt.datetime.fromisoformat(raw) if raw else None

    def set_last_event_check(self, when: dt.datetime | None = None) -> None:
        data = self._events()
        data["last_check"] = (when or dt.datetime.now(dt.timezone.utc)).isoformat()
        self._save_events(data)

    def autopilot(self) -> bool:
        """Armed state lives in data, not config, so it survives a redeploy
        and can be switched off from the dashboard without a rebuild."""
        return bool(self._events().get("autopilot", False))

    def set_autopilot(self, armed: bool) -> None:
        data = self._events()
        data["autopilot"] = bool(armed)
        data["autopilot_changed_at"] = _now()
        self._save_events(data)

    def record_auto_trade(self, symbol: str) -> None:
        data = self._events()
        data.setdefault("auto_trades", []).insert(
            0, {"symbol": symbol.upper(), "at": _now()})
        del data["auto_trades"][200:]
        data.setdefault("cooldowns", {})[symbol.upper()] = _now()
        self._save_events(data)

    def auto_trades_today(self) -> int:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        return sum(1 for t in self._events().get("auto_trades", [])
                   if str(t.get("at", "")).startswith(today))

    def cooling_down(self, symbol: str, minutes: int) -> bool:
        raw = self._events().get("cooldowns", {}).get(symbol.upper())
        if not raw:
            return False
        last = dt.datetime.fromisoformat(raw)
        age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60.0
        return age < minutes

    def opened_on(self, symbol: str) -> dt.date | None:
        """When the position was first opened, for the minimum holding check."""
        entry = self.prior_thesis(symbol)
        raw = entry.get("opened_on")
        try:
            return dt.date.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def held_days(self, symbol: str) -> int | None:
        opened = self.opened_on(symbol)
        return (dt.datetime.now(dt.timezone.utc).date() - opened).days if opened else None


def proposal_id(run_id: str, symbol: str) -> str:
    """Stable and readable - the same name in the same run always has one id."""
    return f"{run_id}:{symbol.replace('/', '-')}"
