"""
Config loading. Every knob in the system lives in config/*.yaml and is read
through here, so behaviour changes are a diff on a YAML file rather than a
code change.

`${VAR}` and `${VAR:-default}` in any string value are expanded from the
environment at load time. Missing variables resolve to "" rather than raising,
so the registry can decide to fall back to the mock broker instead of crashing.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
PROMPT_DIR = ROOT / "prompts"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    pass


def _expand(node: Any) -> Any:
    if isinstance(node, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) or ""), node
        )
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    return node


def load(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml with environment expansion."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    return _expand(raw)


@lru_cache(maxsize=None)
def universe() -> dict[str, Any]:
    cfg = load("universe")
    classes = cfg.get("classes") or {}
    if not classes:
        raise ConfigError("universe.yaml defines no asset classes")
    for key, spec in classes.items():
        if not spec.get("symbols"):
            raise ConfigError(f"universe class '{key}' has no symbols")
    return cfg


@lru_cache(maxsize=None)
def risk() -> dict[str, Any]:
    cfg = load("risk")
    limits = cfg.get("limits") or {}
    for required in ("max_single_position", "min_cash", "max_gross_exposure"):
        if required not in limits:
            raise ConfigError(f"risk.yaml is missing limits.{required}")
    if limits["min_cash"] + limits["max_gross_exposure"] > 1.0 + 1e-9:
        raise ConfigError(
            "risk.yaml: min_cash + max_gross_exposure exceeds 1.0 - "
            "no allocation could ever satisfy both"
        )
    return cfg


@lru_cache(maxsize=None)
def scoring() -> dict[str, Any]:
    cfg = load("scoring")
    weights = cfg.get("weights") or {}
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ConfigError(f"scoring.yaml weights sum to {total}, must be 1.0")
    return cfg


@lru_cache(maxsize=None)
def brokers() -> dict[str, Any]:
    return load("brokers")


@lru_cache(maxsize=None)
def basket() -> dict[str, Any]:
    """
    Sleeve targets must fit inside what the risk limits allow to be deployed.

    Checked here rather than discovered at build time, because the builder
    would otherwise scale every holding down proportionally and produce a
    basket that looks deliberate but is quietly a few points off every target.
    """
    cfg = load("basket")
    sleeves = cfg.get("sleeves") or {}
    if not sleeves:
        raise ConfigError("basket.yaml defines no sleeves")

    total = sum(float(s.get("target", 0)) for s in sleeves.values())
    cash_min = float((cfg.get("cash") or {}).get("min", 0.0))
    gross_max = float(risk()["limits"]["max_gross_exposure"])
    budget = min(1.0 - cash_min, gross_max)

    if total > budget + 1e-9:
        raise ConfigError(
            f"basket.yaml sleeve targets sum to {total:.2f}, above the "
            f"{budget:.2f} that may be deployed (cash floor {cash_min:.2f}, "
            f"max gross exposure {gross_max:.2f})")

    known = set(universe()["classes"])
    for key, spec in sleeves.items():
        unknown = [c for c in spec.get("classes", []) if c not in known]
        if unknown:
            raise ConfigError(
                f"basket sleeve '{key}' references unknown universe "
                f"classes: {', '.join(unknown)}")
    return cfg


def all_symbols() -> list[str]:
    """Every symbol in the universe, plus the benchmark, deduped and ordered."""
    seen: dict[str, None] = {}
    for spec in universe()["classes"].values():
        for sym in spec["symbols"]:
            seen[sym.upper()] = None
    bench = universe().get("benchmark")
    if bench:
        seen[bench.upper()] = None
    return list(seen)


def screen_only_classes() -> set[str]:
    """
    Classes that exist to be screened, not continuously watched.

    The expensive per-symbol work in this system (insider filings, committee
    sittings, news) does not scale to several hundred names: Form 4 alone is
    several SEC requests per symbol, and a committee rotation over five hundred
    instruments would take a fortnight to come round. So a broad class can be
    marked `screen_only`, which keeps it in scoring and in the basket's
    candidate pool while leaving it out of the per-name work.
    """
    return {key for key, spec in universe()["classes"].items()
            if spec.get("screen_only")}


def active_symbols(held: Iterable[str] | None = None) -> list[str]:
    """
    The symbols worth spending real work on: everything except the screen-only
    classes, plus anything currently held.

    A screened name that actually gets bought stops being a candidate and
    becomes a position, and a position is watched properly. Without that
    second half, buying something out of the broad list would leave it with no
    insider data and no committee opinion for as long as it was held.
    """
    skip = screen_only_classes()
    out = [s for s in all_symbols() if class_of(s) not in skip]
    if held:
        seen = set(out)
        out += [s.upper() for s in held if s.upper() not in seen]
    return out


def class_of(symbol: str) -> str:
    """Which universe class a symbol belongs to. Benchmark maps to us_stocks."""
    target = symbol.upper()
    for key, spec in universe()["classes"].items():
        if target in {s.upper() for s in spec["symbols"]}:
            return key
    return "us_stocks" if target == (universe().get("benchmark") or "").upper() else "unknown"


def symbols_for(asset_class: str) -> list[str]:
    spec = universe()["classes"].get(asset_class)
    return [s.upper() for s in spec["symbols"]] if spec else []


def reset_cache() -> None:
    """Used by tests that write temporary config."""
    for fn in (universe, risk, scoring, brokers, basket):
        fn.cache_clear()
