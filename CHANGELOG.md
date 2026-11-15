# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] - 2026-11-15

### Added
- pyproject.toml with ruff, mypy, pytest and coverage configuration.
- CI workflow: lint, format check, type check and coverage-gated tests on
  Python 3.11, 3.12 and 3.13.
- Test coverage for connectors (parsing, symbol mapping, backoff, rate
  limiter), detector (pairing, staleness, spike filter), storage (batching,
  CSV export, stats) and models (sanitization, derived properties).
- README sections for testing and code-quality tooling.

### Changed
- fmt_quote now scales millions before thousands, so large liquidity values
  render as `2.5M` instead of `2500.0K`.
- Internal modernization: `enum.StrEnum`, `datetime.UTC`, `TimeoutError`
  alias and typed narrowing in the detector; behavior unchanged.

## [1.0.0] - 2026-11-14

### Added
- Cross-exchange orderbook polling for Binance, Kraken and Bybit.
- Decimal depth-walk VWAP and slippage calculator.
- Cross-exchange and triangular route evaluation net of fees.
- Rolling-median spike filter and staleness guard.
- Async SQLite persistence with CSV export and session stats.
- Rich live terminal dashboard.
