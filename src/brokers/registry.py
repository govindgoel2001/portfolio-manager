"""
Broker registry and routing.

Adapters are discovered by name: a broker whose config says `adapter: dhan`
causes `src.brokers.dhan` to be imported and the single Broker subclass in it
to be instantiated. There is no hardcoded list to edit - dropping the file in
and adding the YAML entry is the whole integration.

Routing: universe.yaml gives each asset class a broker key. If that broker is
disabled, unimplemented or missing credentials, the class falls back to the
broker named by `fallback` in brokers.yaml (the mock) and the degradation is
reported rather than hidden.

    python -m src.brokers.registry            # show routing + health
    python -m src.brokers.registry --check    # verify every adapter's protocol
"""
from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from .. import config
from .base import Broker, BrokerError, BrokerHealth, verify_adapter, verify_instance

log = logging.getLogger(__name__)


class Registry:
    def __init__(self, *, force_mock: bool = False) -> None:
        self._cfg = config.brokers()
        self._force_mock = force_mock
        self._instances: dict[str, Broker] = {}
        self._failures: dict[str, str] = {}
        self._fallback_key: str = self._cfg.get("fallback", "mock")

    # -- construction -----------------------------------------------------

    def _build(self, key: str) -> Broker:
        spec = (self._cfg.get("brokers") or {}).get(key)
        if spec is None:
            raise BrokerError(f"brokers.yaml has no broker named '{key}'")
        if not spec.get("enabled", True):
            raise BrokerError(f"broker '{key}' is disabled in brokers.yaml")

        adapter = spec.get("adapter")
        if not adapter:
            raise BrokerError(f"broker '{key}' does not name an adapter")

        cls = load_adapter(adapter)
        supports = tuple(spec.get("supports") or ())
        creds = spec.get("credentials") or {}
        endpoints = spec.get("endpoints") or {}
        mode = spec.get("mode", "paper")

        if adapter == "mock":
            return cls(key, seed=int(spec.get("seed", 20260826)), supports=supports)

        if adapter == "alpaca":
            return cls(
                key,
                creds.get("key_id", ""),
                creds.get("secret", ""),
                mode=mode,
                trading_url=endpoints.get("trading", "https://paper-api.alpaca.markets"),
                data_url=endpoints.get("data", "https://data.alpaca.markets"),
                supports=supports,
                feed=spec.get("feed", "iex"),
            )

        # Convention for adapters added later: accept the config spec directly.
        # A new adapter only has to expose __init__(name, spec) to work here.
        return cls(key, spec)

    def _checked(self, broker: Broker) -> Broker:
        problems = verify_instance(broker)
        if problems:
            raise BrokerError("; ".join(problems))
        return broker

    def get(self, key: str) -> Broker:
        if self._force_mock:
            key = self._fallback_key
        if key in self._instances:
            return self._instances[key]
        try:
            broker = self._checked(self._build(key))
        except (BrokerError, ImportError, TypeError) as e:
            if key == self._fallback_key:
                raise
            self._failures[key] = str(e)
            log.warning("broker '%s' unavailable (%s) - falling back to '%s'",
                        key, e, self._fallback_key)
            broker = self.get(self._fallback_key)
        self._instances[key] = broker
        return broker

    # -- routing ----------------------------------------------------------

    def route(self) -> dict[str, str]:
        """asset class -> configured broker key (before any fallback)."""
        return {
            name: spec.get("broker", self._fallback_key)
            for name, spec in config.universe()["classes"].items()
        }

    def for_class(self, asset_class: str) -> Broker:
        return self.get(self.route().get(asset_class, self._fallback_key))

    def for_symbol(self, symbol: str) -> Broker:
        return self.for_class(config.class_of(symbol))

    def active(self) -> dict[str, Broker]:
        """Every broker actually in use, keyed by its live name."""
        out: dict[str, Broker] = {}
        for asset_class in self.route():
            broker = self.for_class(asset_class)
            out[broker.name] = broker
        return out

    def by_broker(self, symbols: list[str]) -> dict[str, tuple[Broker, list[str]]]:
        """Group symbols by the broker that will serve them - one call per venue."""
        grouped: dict[str, tuple[Broker, list[str]]] = {}
        for sym in symbols:
            broker = self.for_symbol(sym)
            entry = grouped.setdefault(broker.name, (broker, []))
            entry[1].append(sym)
        return grouped

    @property
    def failures(self) -> dict[str, str]:
        """Brokers that were configured but could not be built, and why."""
        return dict(self._failures)

    def health(self) -> list[BrokerHealth]:
        return [b.health() for b in self.active().values()]

    def degraded(self) -> bool:
        # active() forces every route to resolve first. Without it, _failures is
        # still empty on a fresh registry and this would always report healthy.
        healths = [b.health() for b in self.active().values()]
        return bool(self._failures) or any(not h.ok for h in healths)


def load_adapter(adapter: str) -> type[Broker]:
    """Import src.brokers.<adapter> and return its Broker subclass."""
    try:
        module = importlib.import_module(f"{__package__}.{adapter}")
    except ImportError as e:
        raise BrokerError(
            f"no adapter module for '{adapter}' - "
            f"create src/brokers/{adapter}.py implementing Broker"
        ) from e

    candidates = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Broker) and obj is not Broker
        and obj.__module__ == module.__name__
    ]
    if len(candidates) != 1:
        raise BrokerError(
            f"src/brokers/{adapter}.py must define exactly one Broker subclass, "
            f"found {len(candidates)}"
        )
    problems = verify_adapter(candidates[0])
    if problems:
        raise BrokerError(f"adapter '{adapter}' is incomplete: " + "; ".join(problems))
    return candidates[0]


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Inspect broker routing and health")
    ap.add_argument("--check", action="store_true",
                    help="verify every configured adapter satisfies the protocol")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg = config.brokers()
    failed = False

    if args.check:
        print("Adapter protocol check")
        for key, spec in (cfg.get("brokers") or {}).items():
            adapter = spec.get("adapter", "?")
            try:
                cls = load_adapter(adapter)
                print(f"  PASS  {key:16s} adapter={adapter:12s} -> {cls.__name__}")
            except BrokerError as e:
                failed = True
                print(f"  FAIL  {key:16s} adapter={adapter:12s} {e}")
        return 1 if failed else 0

    reg = Registry()
    print("Routing")
    for asset_class, key in reg.route().items():
        actual = reg.for_class(asset_class)
        note = "" if actual.name == key else f"  (fell back from '{key}')"
        print(f"  {asset_class:12s} -> {actual.name:16s} [{actual.adapter}/{actual.mode}]{note}")

    print("\nHealth")
    for h in reg.health():
        print(f"  {'OK  ' if h.ok else 'DOWN'}  {h.name:16s} {h.detail}")
        failed = failed or not h.ok

    if reg.failures:
        print("\nUnavailable")
        for key, why in reg.failures.items():
            print(f"  {key:16s} {why}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
