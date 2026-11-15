"""Depth-walk VWAP and arbitrage route math (pure Decimal, no I/O).

Formulas (quote = counter-currency, base = traded asset):
    vwap            = sum(p_i * a_i) / sum(a_i)          over consumed levels
    slippage_bps    = (vwap - reference) / reference * 10_000
    walk by quote   : consume levels until sum(p_i * a_i) >= target_quote
    walk by base    : consume levels until sum(a_i) >= target_base
Partial last level is scaled pro-rata, so filled_quote never overshoots the target.
"""

from __future__ import annotations

from decimal import Decimal

from core.models import (
    ArbitrageOpportunity,
    FeeSchedule,
    FillResult,
    OrderBookLevel,
    OrderBookSnapshot,
    RouteType,
)

BPS = Decimal(10_000)


def walk_depth(levels: list[OrderBookLevel], target_quote: Decimal) -> FillResult:
    """Walk levels accumulating notional until target_quote is consumed."""
    if not levels or target_quote <= 0:
        return FillResult(
            requested_quote=target_quote,
            filled_quote=Decimal(0),
            base_filled=Decimal(0),
            vwap_price=None,
            levels_consumed=0,
            fill_ratio=Decimal(0),
            residual_quote=target_quote,
        )
    filled_quote = Decimal(0)
    base_filled = Decimal(0)
    consumed = 0
    for level in levels:
        remaining = target_quote - filled_quote
        level_notional = level.notional
        take_base = level.amount if level_notional <= remaining else remaining / level.price
        filled_quote += take_base * level.price
        base_filled += take_base
        consumed += 1
        if filled_quote >= target_quote:
            break
    return FillResult(
        requested_quote=target_quote,
        filled_quote=filled_quote,
        base_filled=base_filled,
        vwap_price=filled_quote / base_filled if base_filled > 0 else None,
        levels_consumed=consumed,
        fill_ratio=min(Decimal(1), filled_quote / target_quote),
        residual_quote=max(Decimal(0), target_quote - filled_quote),
    )


def walk_depth_base(levels: list[OrderBookLevel], target_base: Decimal) -> FillResult:
    """Walk levels accumulating base until target_base is filled (sell-side walk)."""
    if not levels or target_base <= 0:
        return FillResult(
            requested_quote=Decimal(0),
            filled_quote=Decimal(0),
            base_filled=Decimal(0),
            vwap_price=None,
            levels_consumed=0,
            fill_ratio=Decimal(0),
            residual_quote=Decimal(0),
        )
    filled_quote = Decimal(0)
    base_filled = Decimal(0)
    consumed = 0
    for level in levels:
        remaining = target_base - base_filled
        take_base = min(level.amount, remaining)
        filled_quote += take_base * level.price
        base_filled += take_base
        consumed += 1
        if base_filled >= target_base:
            break
    vwap = filled_quote / base_filled if base_filled > 0 else None
    return FillResult(
        requested_quote=target_base * vwap if vwap is not None else Decimal(0),
        filled_quote=filled_quote,
        base_filled=base_filled,
        vwap_price=vwap,
        levels_consumed=consumed,
        fill_ratio=min(Decimal(1), base_filled / target_base),
        residual_quote=(target_base - base_filled) * vwap if vwap is not None else Decimal(0),
    )


def slippage_bps(vwap: Decimal, reference: Decimal) -> Decimal:
    """Signed VWAP deviation from a top-of-book reference, in basis points."""
    if reference == 0:
        return Decimal(0)
    return (vwap - reference) / reference * BPS


def evaluate_cross_exchange(
    buy_snap: OrderBookSnapshot,
    sell_snap: OrderBookSnapshot,
    volume_quote: Decimal,
    buy_fee: FeeSchedule,
    sell_fee: FeeSchedule,
) -> ArbitrageOpportunity | None:
    """Buy base on exchange A asks, sell the filled base on exchange B bids, taker fees both legs.

    gross_spread_bps = (bid_B - ask_A) / ask_A * 10_000
    slippage_bps     = (vwap_buy - ask_A) / ask_A * 10_000      (buy walk cost)
                     + (bid_B - vwap_sell) / bid_B * 10_000      (sell walk cost)
    fees_bps         = taker_A + taker_B
    net_spread_bps   = gross - slippage - fees
    net_delta_quote  = proceeds_gross * (1 - taker_B/1e4) - cost_gross * (1 + taker_A/1e4)
    """
    if not buy_snap.asks or not sell_snap.bids:
        return None
    top_ask = buy_snap.asks[0].price
    top_bid = sell_snap.bids[0].price
    if top_ask <= 0 or top_bid <= 0:
        return None

    buy_fill = walk_depth(buy_snap.asks, volume_quote)
    if buy_fill.base_filled <= 0 or buy_fill.vwap_price is None:
        return None
    sell_fill = walk_depth_base(sell_snap.bids, buy_fill.base_filled)
    if sell_fill.base_filled <= 0 or sell_fill.vwap_price is None:
        return None

    gross = (top_bid - top_ask) / top_ask * BPS
    buy_slip = slippage_bps(buy_fill.vwap_price, top_ask)
    sell_slip = -slippage_bps(sell_fill.vwap_price, top_bid)
    slip = buy_slip + sell_slip
    fees = buy_fee.taker_bps + sell_fee.taker_bps
    net = gross - slip - fees

    buy_fee_mult = Decimal(1) + buy_fee.taker_bps / BPS
    sell_fee_mult = Decimal(1) - sell_fee.taker_bps / BPS
    net_delta = sell_fill.filled_quote * sell_fee_mult - buy_fill.filled_quote * buy_fee_mult

    return ArbitrageOpportunity(
        route=RouteType.CROSS_EXCHANGE,
        pair=buy_snap.pair,
        buy_exchange=buy_snap.exchange,
        sell_exchange=sell_snap.exchange,
        volume_quote=volume_quote,
        gross_spread_bps=gross,
        slippage_bps=slip,
        fees_bps=fees,
        net_spread_bps=net,
        net_delta_quote=net_delta,
        fill_ratio=min(buy_fill.fill_ratio, sell_fill.fill_ratio),
        buy_latency_ms=buy_snap.latency_ms,
        sell_latency_ms=sell_snap.latency_ms,
    )


def evaluate_triangular(
    snap_a: OrderBookSnapshot,
    snap_b: OrderBookSnapshot,
    snap_c: OrderBookSnapshot,
    volume_quote: Decimal,
    fees: FeeSchedule,
) -> ArbitrageOpportunity | None:
    """Closed-loop walk on one exchange through pair roles (base/quote, mid/base, mid/quote).

    Loop (e.g. BTC/USDT, ETH/BTC, ETH/USDT starting in USDT):
        leg 1: quote -> base   at ask_a,   base_1 = Q / vwap_a          (buy base)
        leg 2: base  -> mid    at ask_b,   mid_2 = base_1 / vwap_b      (buy mid, price in base)
        leg 3: mid   -> quote  at bid_c,   Q' = mid_2 * vwap_c          (sell mid)
    Fees: each leg's taker fee scales the received amount by (1 - f/1e4).
        realized_net   = Q' * (1-f3) / Q  where each leg pre-fee uses its VWAP
        implied        = bid_c / (ask_a * ask_b)                        (top-of-book)
        gross_spread_bps = (implied - 1) * 10_000
        slippage_bps     = (implied - realized_gross) * 10_000
        net_spread_bps   = gross - slippage - 3 * taker_bps
    Returns None when any leg cannot be filled.
    """
    if not snap_a.asks or not snap_b.asks or not snap_c.bids:
        return None
    top_ask_a = snap_a.asks[0].price
    top_ask_b = snap_b.asks[0].price
    top_bid_c = snap_c.bids[0].price
    if min(top_ask_a, top_ask_b, top_bid_c) <= 0:
        return None

    leg1 = walk_depth(snap_a.asks, volume_quote)
    if leg1.base_filled <= 0 or leg1.vwap_price is None:
        return None
    leg2 = walk_depth(snap_b.asks, leg1.base_filled)
    if leg2.base_filled <= 0 or leg2.vwap_price is None:
        return None
    leg3 = walk_depth_base(snap_c.bids, leg2.base_filled)
    if leg3.base_filled <= 0 or leg3.vwap_price is None:
        return None

    f = fees.taker_bps / BPS
    proceeds_net = leg3.filled_quote * (1 - f)

    implied = top_bid_c / (top_ask_a * top_ask_b)
    realized_gross = leg3.filled_quote / leg1.filled_quote
    gross = (implied - 1) * BPS
    slip = (implied - realized_gross) * BPS
    total_fees = fees.taker_bps * 3
    net = gross - slip - total_fees

    return ArbitrageOpportunity(
        route=RouteType.TRIANGULAR,
        pair=f"{snap_a.pair}->{snap_b.pair}->{snap_c.pair}",
        buy_exchange=snap_a.exchange,
        sell_exchange=snap_c.exchange,
        volume_quote=volume_quote,
        gross_spread_bps=gross,
        slippage_bps=slip,
        fees_bps=total_fees,
        net_spread_bps=net,
        net_delta_quote=proceeds_net - volume_quote * (1 + f),
        fill_ratio=min(leg1.fill_ratio, leg2.fill_ratio, leg3.fill_ratio),
        buy_latency_ms=snap_a.latency_ms,
        sell_latency_ms=snap_c.latency_ms,
    )
