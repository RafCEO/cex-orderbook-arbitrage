# cex-orderbook-arbitrage

[![CI](https://github.com/RafCEO/cex-orderbook-arbitrage/actions/workflows/ci.yml/badge.svg)](https://github.com/RafCEO/cex-orderbook-arbitrage/actions/workflows/ci.yml)

Cross-exchange crypto spread monitor and orderbook depth analyzer with an
optional execution layer. Polls public REST orderbooks from Binance, Kraken,
Bybit, OKX, KuCoin and Gate.io (USDT-quoted) plus bitFlyer, bitbank and
Coincheck (JPY-quoted), walks real depth to compute volume-weighted fill
prices, and evaluates cross-exchange and triangular routes net of taker fees,
slippage and stale-data risk. Japanese books join the same comparison through
a market-implied FX cross (no external FX feed). Monitor mode is read-only
analytics on public data; the `paper`, `testnet` and `live` modes add an
execution layer on top of the same detection pipeline.

## Exchange coverage

| Exchange | Quote | Pairs (examples) | Fee defaults (maker/taker bps) | Notes |
|---|---|---|---|---|
| binance | USDT | BTC/USDT, ETH/USDT, ETH/BTC | 10/10 | live/testnet trading supported |
| kraken | USDT/USD/EUR | BTC/USDT, ETH/USDT | 16/26 | trading intentionally unsupported |
| bybit | USDT | BTC/USDT, ETH/USDT, ETH/BTC | 10/10 | live/testnet trading supported |
| okx | USDT | BTC/USDT, ETH/USDT | 8/10 | |
| kucoin | USDT | BTC/USDT, ETH/USDT | 10/10 | `/level2_100` endpoint, sliced locally |
| gate | USDT | BTC/USDT, ETH/USDT | 16/20 | |
| bitflyer | JPY | BTC/JPY, ETH/JPY | 12/12 | no USDT pairs; no server timestamp |
| bitbank | JPY | BTC/JPY, ETH/JPY | 12/12 | lower-case symbol path |
| coincheck | JPY | BTC/JPY, ETH/JPY | 15/25 | levels may arrive unsorted |

Fee defaults are documented approximations: verify them against your tier,
volume and any VIP/rebate structure before acting on reported net spreads.

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
                     +---------------+----------------+
                     v                                v
        +--------------------------+    +---------------------------+
        |      cli/dashboard.py    |    |        execution/         |
        | rich Live table, status  |    | risk.py pre-trade gate +  |
        | lines, latency coloring; |    | daily-loss breaker, then  |
        | trading panel and fills  |    | paper or live executor,   |
        | feed when trading        |    | portfolio + trades_logger |
        +--------------------------+    +---------------------------+
```

### Async runtime and per-tick data flow

The process runs on a single asyncio event loop started by `asyncio.run` in
`main.py`. One shared `aiohttp.ClientSession` (pooled TCP connector) serves
every connector fetch and every signed executor request; it is closed once at
shutdown. Each tick:

1. **Fetch.** `asyncio.gather` launches one `fetch_orderbook` coroutine per
   (exchange, pair) concurrently. Before each request a connector acquires a
   token from its per-exchange token bucket (`rate_limit_per_sec`); transport
   errors and HTTP >= 400 responses are retried with full-jitter exponential
   backoff (0.5 s base, x2 per attempt, 30 s cap, 5 attempts, 10 s request
   timeout). A failed fetch is logged and skipped; it never aborts the tick.
2. **Detect.** Snapshots younger than `max_snapshot_age_ms` flow through
   `core/models.py` into the pure engine: `vwap_calculator.py` walks depth,
   and `detector.py` enumerates cross-exchange and triangular routes and
   applies the rolling-median spike filter.
3. **Execute (trading modes only).** Confirmed cross-exchange opportunities
   pass the risk guard (per-trade cap, open-exposure cap, spread gate,
   daily-loss circuit breaker) before the paper or live executor fills both
   legs; fills update the portfolio and are recorded by the trades logger.
   Triangular routes are monitor-only.
4. **Persist.** Opportunities and trade reports are enqueued on an async
   queue; a background drainer task batches INSERTs into SQLite (WAL mode),
   so persistence never blocks the poll loop.
5. **Render.** The rich `Live` display refreshes: spread table and exchange
   status lines in all modes, plus the positions/P&L panel and fills feed
   when trading. The loop then sleeps `interval - elapsed`.

## Module map

| Module | Responsibility |
|---|---|
| `core/models.py` | Pydantic v2 strict domain models with Decimal math and derived properties |
| `connectors/base.py` | Abstract REST connector: token-bucket rate limiter, full-jitter retry backoff, transport latency tracking |
| `connectors/binance.py` | Binance spot depth endpoint |
| `connectors/bybit.py` | Bybit v5 spot orderbook endpoint |
| `connectors/kraken.py` | Kraken public depth endpoint with XBT/BTC normalization |
| `connectors/okx.py` | OKX v5 books endpoint (dash symbols, string ts) |
| `connectors/kucoin.py` | KuCoin level2 endpoint (level2_100 + local slice) |
| `connectors/gate.py` | Gate.io v4 order book endpoint (underscore symbols, ms timestamps) |
| `connectors/bitflyer.py` | bitFlyer board endpoint (JPY, numeric levels, receive-time ts) |
| `connectors/bitbank.py` | bitbank public depth (JPY, lower-case path, ms timestamp) |
| `connectors/coincheck.py` | Coincheck order books (JPY, unsorted levels, receive-time ts) |
| `engine/vwap_calculator.py` | Depth-walk VWAP, slippage, cross-exchange, cross-quote and triangular route math |
| `engine/detector.py` | Latency guard, per-base route enumeration, rolling-median spike filter |
| `engine/fx.py` | Implied FX crosses: mid ratio, median smoothing, staleness and sanity guards |
| `execution/base.py` | OrderIntent / ExecutionReport models, executor contract, intent builder |
| `execution/auth.py` | Pure signing functions for Binance, Bybit and Kraken private endpoints |
| `execution/paper_executor.py` | Simulated two-leg fills against live fetched orderbooks |
| `execution/live_executor.py` | Signed LIMIT IOC order placement (Binance + Bybit, testnet and live) |
| `execution/portfolio.py` | Virtual balances, inventory with cost basis, realized/unrealized P&L |
| `execution/risk.py` | Pre-trade caps, spread gate, daily-loss circuit breaker |
| `storage/logger.py` | Async queued SQLite persistence, session stats, CSV export |
| `storage/trades_logger.py` | Same pattern for executed trades, stats and CSV export |
| `cli/dashboard.py` | rich Live terminal dashboard |
| `cli/trading_panel.py` | Positions/P&L panel, fills feed, HALTED state |
| `main.py` | CLI, config precedence, poll loop, trading pipeline, graceful shutdown |

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

**Implied FX cross.** Japanese exchanges quote BTC/JPY while global exchanges
quote BTC/USDT; a direct price comparison across those quotes is meaningless.
For every ordered quote pair the engine derives a market-implied rate from a
reference base traded against both quotes:

```
rate(JPY/USDT) = mid(BTC/JPY) / mid(BTC/USDT)     freshest snapshots
smoothed rate  = rolling median over last N ticks (default 5)
```

Two guards protect route economics. A staleness guard (same `max_age_ms` as
snapshots) refuses to use a cross whose inputs or last update are too old. A
sanity band marks the cross degraded when a raw reading deviates more than
`sanity_band_bps` (default 200) from the smoothing history's median; while
degraded, cross-quote routes stop being emitted so a broken conversion never
looks like free money. Conversion is not free: a configurable haircut
(`conversion_haircut_bps`, default 25, approximating stablecoin/fiat
conversion cost) is charged exactly once on the foreign leg. For a route
buying in the common quote (USDT) and selling in the foreign quote:

```
normalized sell price = p_foreign * rate * (1 - haircut/1e4)
```

For the reverse direction the foreign buy cost is grossed up by the same
haircut. Cross-quote opportunities carry `fx_rate` and `fx_quote` (e.g.
JPY/USDT) so the dashboard, SQLite tables and CSV exports show when a route
rides an FX conversion. Same-quote routes are bit-for-bit unaffected.

Paper execution across quotes fills both legs natively: buy BTC/JPY with JPY
balances on the Japanese exchange, sell BTC/USDT on the global one. The round
trip leaves inventory in the other quote currency; portfolio equity marks it
through the same implied rate. Triangular routes stay same-quote only.

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
| `--export PATH.csv` | off | Dump this run's rows to CSV at exit |
| `--interval` | config `poll_interval` | Seconds between polling ticks |
| `--duration` | `0` | Stop after N seconds; 0 runs until Ctrl+C |
| `--config` | `config.yaml` | Config file path |
| `--exchanges` | all enabled | Comma-separated subset, e.g. `binance,bybit` |
| `--mode` | `monitor` | `monitor` (read-only), `paper` (simulated fills), `testnet`, `live` |
| `--ack-live-risk` | off | Required acknowledgement to start live mode |

Precedence: CLI flags override `config.yaml`; `config.yaml` overrides built-in
defaults.

Example smoke run:

```
python main.py --pairs BTC/USDT,ETH/USDT --volume 1000 --duration 20 --export /tmp/session.csv
```

## config.yaml reference

```yaml
exchanges:
  binance:   { enabled: true, maker_fee_bps: 10, taker_fee_bps: 10, rate_limit_per_sec: 10 }
  kraken:    { enabled: true, maker_fee_bps: 16, taker_fee_bps: 26, rate_limit_per_sec: 1 }
  bybit:     { enabled: true, maker_fee_bps: 10, taker_fee_bps: 10, rate_limit_per_sec: 5 }
  okx:       { enabled: true, maker_fee_bps: 8,  taker_fee_bps: 10, rate_limit_per_sec: 10 }
  kucoin:    { enabled: true, maker_fee_bps: 10, taker_fee_bps: 10, rate_limit_per_sec: 8 }
  gate:      { enabled: true, maker_fee_bps: 16, taker_fee_bps: 20, rate_limit_per_sec: 8 }
  bitflyer:  { enabled: true, maker_fee_bps: 12, taker_fee_bps: 12, rate_limit_per_sec: 1.5 }
  bitbank:   { enabled: true, maker_fee_bps: 12, taker_fee_bps: 12, rate_limit_per_sec: 5 }
  coincheck: { enabled: true, maker_fee_bps: 15, taker_fee_bps: 25, rate_limit_per_sec: 1.5 }
poll_interval: 2.0
default_volume_quote: 1000
min_spread_bps: 5.0
depth_levels: 50
max_snapshot_age_ms: 5000
db_path: data/session.db
spike_filter: { window: 20, deviation_bps: 25, confirmations: 2 }
triangular_triplets:
  - [BTC/USDT, ETH/BTC, ETH/USDT]
fx:
  reference_base: BTC
  smoothing_window: 5
  sanity_band_bps: 200
  conversion_haircut_bps: 25
  max_age_ms: 5000
```

`triangular_triplets` entries are `[base/quote, mid/base, mid/quote]` roles;
each triplet is evaluated on every exchange that lists all three pairs. The
default `--pairs` stays `BTC/USDT,ETH/USDT`; add `BTC/JPY` (and `ETH/JPY`) to
engage the Japanese books and the implied FX cross. Quote currencies are
per-exchange properties; the portfolio's `starting_balances` are denominated
in each exchange's own quote (USDT exchanges in USDT, Japanese ones in JPY).

## Trading modes

`--mode` selects how detected opportunities are handled:

| Mode | Behavior |
|---|---|
| `monitor` | Default. Read-only spread monitoring, byte-for-byte the original behavior. |
| `paper` | Confirmed opportunities pass a risk gate, then fill against the live fetched orderbooks: buy leg walks real asks, sell leg walks real bids, taker fees per exchange apply, a virtual portfolio tracks balances and P&L. No keys, no orders, fills clearly labeled simulated. |
| `testnet` | Same pipeline, but legs execute as signed LIMIT IOC orders on Binance/Bybit testnet. Exchanges without credentials degrade to monitor-only with a warning. |
| `live` | Real orders with real funds. Refuses to start without env credentials AND `--ack-live-risk`; prints a risk panel at startup. |

Kraken trading is intentionally unsupported: it offers no testnet, so the risk
of shipping untested signing against live capital is not taken. Kraken books
still participate in monitoring and paper mode.

Credentials are read exclusively from the environment and never written
anywhere: `BINANCE_API_KEY`/`BINANCE_API_SECRET` and
`BYBIT_API_KEY`/`BYBIT_API_SECRET`. Live orders default to LIMIT IOC at the
walked-VWAP price padded by `price_buffer_bps` for price protection; naked
market orders are an explicit `market_orders: true` escape hatch.

The `trading:` config section controls starting paper balances, per-trade and
exposure caps, the daily-loss circuit breaker, the minimum spread to act on,
and order style:

```yaml
trading:
  mode: monitor
  starting_balances: { binance: 1000, bybit: 1000, bitflyer: 150000 }
  max_trade_quote: 100
  max_exposure_quote: 500
  daily_loss_limit_quote: 50
  min_act_spread_bps: 5.0
  market_orders: false
  price_buffer_bps: 5.0
```

Balances are per exchange in that exchange's quote currency. In paper mode a
cross-quote round trip fills both legs natively (JPY balances buy the JPY
leg), leaves inventory in the other quote, and reports equity in USDT through
the live implied FX rate.

Executed trades persist to a `trades` table in the same SQLite database.
Every row is stamped with a run-scoped `session_id`; the summary panel, stats
and CSV exports cover only the current run, so prior runs in the same database
file never bleed into a session's numbers. With `--export`, trades go to
`trades_<name>.csv` next to the opportunities CSV.


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
confirmation logic, persistence batching, session-scoped stats and CSV
export, dashboard formatting helpers, signing for all three exchanges,
paper fills, live executor order construction and rejection paths, portfolio
math and the risk guard. Runtime is well under a second; the coverage gate
is 85%.

## Code quality and tooling

Configuration lives in `pyproject.toml`:

- `ruff check .` — lint (E, F, W, I, UP, B, SIM rule families)
- `ruff format --check .` — formatting
- `mypy core connectors engine storage cli execution main.py tests` — strict
  typing (`disallow_untyped_defs`, `no_implicit_optional`, `warn_return_any`)
- `pytest --cov` — coverage gate at 85% over core, connectors, engine,
  storage, cli and execution

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
- Kraken order execution is intentionally unsupported.
- This software is not trading or financial advice. Market data may be delayed,
  inaccurate or unavailable; use at your own risk and never risk capital you
  cannot afford to lose.

## License

MIT. See [LICENSE](LICENSE).
