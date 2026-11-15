# cex-orderbook-arbitrage

[![CI](https://github.com/RafCEO/cex-orderbook-arbitrage/actions/workflows/ci.yml/badge.svg)](https://github.com/RafCEO/cex-orderbook-arbitrage/actions/workflows/ci.yml)

Cross-exchange crypto spread monitor and orderbook depth analyzer. Polls public
REST orderbooks from Binance, Kraken and Bybit, walks real depth to compute
volume-weighted fill prices, and evaluates cross-exchange and triangular routes
net of taker fees, slippage and stale-data risk. It is a read-only analytics
and monitoring tool built on public market data: it places no orders, requires
no API keys, and does not execute anything.

## Architecture

```
                +--------------------------------------------------+
                |                    main.py                       |
                |  argparse / config.yaml / asyncio orchestrator   |
                +---------+-------------------------+--------------+
                          |                         |
         per tick gather  v                         v  shutdown
   +----------------------+----------+    +-------------------------+
   |      connectors/     |          |    |    storage/logger.py    |
   |  binance.py          |          |    |  async SQLite (WAL),   |
   |  bybit.py     -----> snapshots  |    |  batched inserts,      |
   |  kraken.py           |          |    |  CSV export, stats     |
   +----------------------+          |    +------------------------+
                                     v
                     +-------------------------------+
                     |          core/models.py      |
                     |  OrderBookSnapshot (sorted,  |
                     |  sanitized), FillResult,     |
                     |  ArbitrageOpportunity        |
                     +---------------+---------------+
                                     |
                                     v
                     +-------------------------------+
                     |         engine/              |
                     |  vwap_calculator.py          |
                     |   walk_depth, slippage,      |
                     |   cross-exchange, triangular |
                     |  detector.py                 |
                     |   latency guard, route scan, |
                     |   rolling-median spike filter|
                     +---------------+---------------+
                                     |
                                     v
                        +--------------------------+
                        |      cli/dashboard.py    |
                        |  rich Live table, status |
                        |  lines, latency coloring |
                        +--------------------------+
```

## Module map

| Module | Responsibility |
|---|---|
| `core/models.py` | Pydantic v2 strict domain models with Decimal math and derived properties |
| `connectors/base.py` | Abstract REST connector: token-bucket rate limiter, full-jitter retry backoff, transport latency tracking |
| `connectors/binance.py` | Binance spot depth endpoint |
| `connectors/bybit.py` | Bybit v5 spot orderbook endpoint |
| `connectors/kraken.py` | Kraken public depth endpoint with XBT/BTC normalization |
| `engine/vwap_calculator.py` | Depth-walk VWAP, slippage, cross-exchange and triangular route math |
| `engine/detector.py` | Latency guard, per-pair route enumeration, rolling-median spike filter |
| `storage/logger.py` | Async queued SQLite persistence, session stats, CSV export |
| `cli/dashboard.py` | rich Live terminal dashboard |
| `main.py` | CLI, config precedence, poll loop, graceful shutdown |

## The math

All arithmetic is `Decimal`; floats appear only at the display/persistence
boundary.

**VWAP depth walk.** Given ordered levels `(p_i, a_i)` and a quote-currency
target `Q`, consume levels while accumulating notional, scaling the last level
pro-rata so the total never overshoots:

```
vwap = sum(p_i * a_i) / sum(a_i)          over consumed (possibly partial) levels
fill_ratio = filled_quote / Q
residual_quote = Q - filled_quote
```

**Slippage.** Signed deviation of the achieved VWAP from a top-of-book
reference `r`, in basis points:

```
slippage_bps = (vwap - r) / r * 10_000
```

**Cross-exchange route.** Buy base on exchange A asks, sell the filled base on
exchange B bids, taker fees on both legs:

```
gross_spread_bps = (bid_B_top - ask_A_top) / ask_A_top * 10_000
slippage_bps     = (vwap_buy - ask_A_top) / ask_A_top * 10_000
                 + (bid_B_top - vwap_sell) / bid_B_top * 10_000
fees_bps         = taker_A + taker_B
net_spread_bps   = gross_spread_bps - slippage_bps - fees_bps
net_delta_quote  = proceeds * (1 - taker_B/10^4) - cost * (1 + taker_A/10^4)
```

**Triangular route.** Closed loop on one exchange through pairs
`(base/quote, mid/base, mid/quote)`, e.g. `BTC/USDT -> ETH/BTC -> ETH/USDT`,
with taker fee applied on each of the three legs:

```
base_1     = Q / vwap_a                 (buy base with quote)
mid_2      = base_1 * (1-f) / vwap_b    (buy mid with base, fee-adjusted)
Q'         = mid_2 * (1-f) * vwap_c     (sell mid back into quote, fee-adjusted)
implied    = bid_c_top / (ask_a_top * ask_b_top)
gross_bps  = (implied - 1) * 10_000
slip_bps   = (implied - realized_gross) * 10_000     where realized = Q'/Q (pre-fee)
net_bps    = gross_bps - slip_bps - 3 * taker_bps
```

**Spike filter.** Per route, keep a rolling window (default 20) of
`net_spread_bps` readings. A reading is suppressed when it deviates from the
rolling median by more than `deviation_bps` (default 25) unless the route has
printed `net >= min_spread_bps` for `confirmations` (default 2) consecutive
ticks, which marks the shift as persistent rather than a stale-quote artifact.

## Quickstart

Python 3.11+ required.

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

No API keys or environment setup are needed; every endpoint used is public and
read-only.

## CLI options

| Flag | Default | Description |
|---|---|---|
| `--pairs` | `BTC/USDT,ETH/USDT` | Comma-separated canonical pairs |
| `--volume` | config `default_volume_quote` | Trade size in quote currency used for depth walks |
| `--min-spread` | config `min_spread_bps` | Minimum net spread (bps) for a highlighted route |
| `--export PATH.csv` | off | Dump the session table to CSV at exit |
| `--interval` | config `poll_interval` | Seconds between polling ticks |
| `--duration` | `0` | Stop after N seconds; 0 runs until Ctrl+C |
| `--config` | `config.yaml` | Config file path |
| `--exchanges` | all enabled | Comma-separated subset, e.g. `binance,bybit` |

Precedence: CLI flags override `config.yaml`; `config.yaml` overrides built-in
defaults.

Example smoke run:

```
python main.py --pairs BTC/USDT,ETH/USDT --volume 1000 --duration 20 --export /tmp/session.csv
```

## config.yaml reference

```yaml
exchanges:
  binance: { enabled: true, maker_fee_bps: 10, taker_fee_bps: 10, rate_limit_per_sec: 10 }
  kraken:  { enabled: true, maker_fee_bps: 16, taker_fee_bps: 26, rate_limit_per_sec: 1 }
  bybit:   { enabled: true, maker_fee_bps: 10, taker_fee_bps: 10, rate_limit_per_sec: 5 }
poll_interval: 2.0
default_volume_quote: 1000
min_spread_bps: 5.0
depth_levels: 50
max_snapshot_age_ms: 5000
db_path: data/session.db
spike_filter: { window: 20, deviation_bps: 25, confirmations: 2 }
triangular_triplets:
  - [BTC/USDT, ETH/BTC, ETH/USDT]
```

`triangular_triplets` entries are `[base/quote, mid/base, mid/quote]` roles;
each triplet is evaluated on every exchange that lists all three pairs.

## Testing

```
pip install -r requirements-dev.txt
python -m pytest --cov --cov-report=term
```

The suite is fully hermetic: hand-crafted orderbooks, scripted aiohttp
sessions, frozen clocks and a temporary SQLite database. No network access.
It covers model sanitization and derived metrics, connector payload parsing
and symbol mapping (including Kraken XBT/XXBTZ normalization), retry backoff
and the token-bucket rate limiter, detector pairing and spike-filter
confirmation logic, persistence batching and CSV export, and the dashboard
formatting helpers. Runtime is well under a second; the coverage gate is 85%.

## Code quality and tooling

Configuration lives in `pyproject.toml`:

- `ruff check .` — lint (E, F, W, I, UP, B, SIM rule families)
- `ruff format --check .` — formatting
- `mypy core connectors engine storage cli main.py tests` — strict typing
  (`disallow_untyped_defs`, `no_implicit_optional`, `warn_return_any`)
- `pytest --cov` — coverage gate at 85% over core, connectors, engine,
  storage and cli

CI ([ci.yml](.github/workflows/ci.yml)) runs all four on Python 3.11, 3.12
and 3.13 for every push and pull request to `main`.

## Docker

```
docker build -t cex-orderbook-arbitrage .
docker run --rm cex-orderbook-arbitrage
```

The container runs as a non-root user and writes its SQLite session file inside
the container filesystem; mount a volume over `/app/data` to persist it.

## Limitations and disclaimer

- REST polling granularity only: no websocket L2 streams, so books are point
  samples, not continuous. Opportunities between ticks are invisible.
- Exchange-reported timestamps may be skewed or absent (Binance's depth
  endpoint may omit `E` under some conditions; Kraken's depth response carries
  no server time, so receive time is used). Latency columns therefore mix
  transport latency and exchange clock skew and can read negative or large.
- Fee schedules are static assumptions; real fills face variable tiers,
  rebates, withdrawal constraints, transfer latency and counterparty risk that
  this tool does not model.
- No order execution of any kind. This software performs no trades and must
  not be construed as trading or financial advice. Market data may be delayed,
  inaccurate or unavailable; use at your own risk.

## License

MIT. See [LICENSE](LICENSE).
