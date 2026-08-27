# Adding a broker

The pipeline, risk engine, dashboard and API never import a concrete broker.
They receive a `Broker` from the registry and never learn which one they got.
So adding Dhan for Indian equities, or Hyperliquid for crypto and perps, is
three steps and touches no existing logic.

## Step 1, write the adapter

Create `src/brokers/<name>.py` with exactly one class that subclasses `Broker`.
The registry finds it by importing the module named in your config and looking
for the single Broker subclass defined there, so the file name and the
`adapter:` key in `config/brokers.yaml` must match.

Nine methods are required. Their types are in `src/brokers/base.py`:

```python
from .base import Account, Bar, Broker, BrokerError, BrokerHealth
from .base import OrderRequest, OrderResult, Position, Quote

class DhanBroker(Broker):
    adapter = "dhan"

    def __init__(self, name: str, spec: dict) -> None:
        # Adapters beyond alpaca and mock receive the raw config block.
        creds = spec.get("credentials", {})
        if not creds.get("access_token"):
            raise BrokerError(f"{name}: DHAN_ACCESS_TOKEN is not set")
        self.name = name
        self.mode = spec.get("mode", "paper")
        self.supports = tuple(spec.get("supports", ()))
        ...

    def health(self) -> BrokerHealth: ...
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_bars(self, symbols, days) -> dict[str, list[Bar]]: ...
    def get_quotes(self, symbols) -> dict[str, Quote]: ...
    def submit_order(self, order: OrderRequest) -> OrderResult: ...
    def get_orders(self, status="all", limit=50) -> list[OrderResult]: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def is_market_open(self, asset_class="us_stocks") -> bool: ...
```

Four rules that matter more than they look.

Translate the vendor's JSON into the dataclasses in `base.py` and never let a
vendor type escape the adapter. The report, the risk gate and the dashboard all
assume `Position.market_value` means the same thing on every venue.

Raise `BrokerError` for anything that fails. Vendor SDK exceptions leaking out
will crash a run instead of degrading it, because the registry only catches
`BrokerError`.

Implement `normalize` and `denormalize` if the vendor spells symbols
differently from `config/universe.yaml`. The Alpaca adapter does this because
positions come back as `BTCUSD` while the universe calls it `BTC/USD`. Getting
this wrong shows up as a position the dashboard cannot match to a class.

Set `supports_notional = False` if the venue only takes quantities. Allocation
is computed in weights, so the adapter is responsible for converting dollars to
a quantity using the last quote, and for saying so in `OrderResult.note`.

## Step 2, register it

Add a block to `config/brokers.yaml`:

```yaml
  dhan:
    adapter: dhan
    enabled: true
    mode: paper
    supports: [in_stocks]
    credentials:
      client_id:    ${DHAN_CLIENT_ID}
      access_token: ${DHAN_ACCESS_TOKEN}
    endpoints:
      trading: https://api.dhan.co
```

`${VAR}` is expanded from the environment when the config loads, so the secret
stays in `.env`.

## Step 3, route asset classes to it

In `config/universe.yaml`, either point an existing class at the new broker or
add a class:

```yaml
  in_stocks:
    label: India
    icon: chart
    broker: dhan
    symbols: [RELIANCE, TCS, HDFCBANK, INFY]
```

The dashboard sidebar is generated from these keys, so a new class appears as a
new tab with no frontend change.

## Verify before you route real symbols

```bash
python -m src.brokers.registry --check   # protocol conformance
python -m src.brokers.registry           # routing and live health
python -m pytest tests/ -q               # the suite checks every configured adapter
python run_daily.py --dry-run            # full pipeline, writes nothing
```

`--check` catches an abstract method left unimplemented. The plain command
shows which class routes to which broker and whether each one answers. A broker
that fails to build falls back to the mock, and the failure is printed rather
than swallowed, so a missing credential degrades the run instead of ending it.

## Notes for specific venues

Hyperliquid trades perpetuals with leverage. `Position.qty` can be negative for
a short, and nothing in the risk engine currently understands negative exposure:
`max_gross_exposure` sums target weights and would read a short as a reduction.
Before routing perps, either constrain the adapter to spot only, or extend
`src/risk.py` to compute gross as the sum of absolute weights. Set
`supports_shorting = True` on the adapter so the gap is visible in config.

Dhan settles in INR while the portfolio reports in USD. `Account.currency`
exists for this, but the report and the dashboard currently assume one currency
across the whole portfolio. Adding a second live currency means deciding where
the conversion happens, and the honest place is the adapter, converting to the
portfolio's base currency and recording the rate it used in `OrderResult.note`.

A broker in `mode: live` is refused auto-execution unconditionally by
`src/risk.py`, and the approve endpoint refuses any broker that is not paper.
That check is deliberately not readable from `risk.yaml`, so no config edit can
turn it off.
