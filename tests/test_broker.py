"""XTB protocol decoding and broker adapter mapping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from unsharp_bot.broker.base import Quote
from unsharp_bot.broker.xtb_broker import XtbBroker, _epoch_ms, drop_forming_candle
from unsharp_bot.broker.xtb_client import (
    CMD_BUY,
    CMD_SELL,
    RateLimiter,
    parse_rate_info,
    to_utc,
)
from unsharp_bot.models import Candle, Direction


def test_parse_rate_info_decodes_scaled_deltas():
    """XTB sends integers scaled by 10^digits, with H/L/C as deltas from open."""
    record = {"ctm": 1_600_000_000_000, "open": 41_000, "high": 120, "low": -80,
              "close": 50, "vol": 12.0}
    parsed = parse_rate_info(record, digits=2)
    assert parsed["open"] == pytest.approx(410.00)
    assert parsed["high"] == pytest.approx(411.20)
    assert parsed["low"] == pytest.approx(409.20)
    assert parsed["close"] == pytest.approx(410.50)
    assert parsed["volume"] == pytest.approx(12.0)


def test_parse_rate_info_five_digit_forex():
    record = {"ctm": 1_600_000_000_000, "open": 108_500, "high": 45, "low": -60, "close": 20}
    parsed = parse_rate_info(record, digits=5)
    assert parsed["open"] == pytest.approx(1.08500)
    assert parsed["high"] == pytest.approx(1.08545)
    assert parsed["low"] == pytest.approx(1.08440)
    assert parsed["close"] == pytest.approx(1.08520)


def test_parse_rate_info_handles_a_bearish_bar():
    """A down bar has a negative close delta; high/low must still bracket it."""
    record = {"ctm": 1_600_000_000_000, "open": 50_000, "high": 30, "low": -200, "close": -150}
    parsed = parse_rate_info(record, digits=2)
    assert parsed["close"] < parsed["open"]
    assert parsed["low"] <= parsed["close"] <= parsed["high"]
    assert parsed["low"] <= parsed["open"] <= parsed["high"]


def test_epoch_conversions_round_trip():
    moment = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    assert to_utc(_epoch_ms(moment)) == moment
    # A naive datetime is treated as UTC.
    assert _epoch_ms(datetime(2026, 9, 14, 13, 30)) == _epoch_ms(moment)


def test_drop_forming_candle_removes_the_open_bar():
    """A bar is closed once open_time + period has passed; the current one is not."""
    now = datetime.now(timezone.utc)
    closed_a = Candle(now - timedelta(minutes=12), 1, 1, 1, 1)   # closed at -7 min
    closed_b = Candle(now - timedelta(minutes=7), 1, 1, 1, 1)    # closed at -2 min
    forming = Candle(now - timedelta(minutes=2), 1, 1, 1, 1)     # closes in 3 min
    kept = drop_forming_candle([closed_a, closed_b, forming], period_minutes=5)
    assert [candle.timestamp for candle in kept] == [closed_a.timestamp, closed_b.timestamp]
    assert drop_forming_candle([], 5) == []
    # A bar whose close is comfortably in the past is always kept.
    assert len(drop_forming_candle([closed_a], 5)) == 1


def test_symbol_spec_mapping():
    spec = XtbBroker._to_symbol_spec({
        "symbol": "US500", "description": "S&P 500", "categoryName": "IND",
        "currency": "USD", "currencyProfit": "USD", "contractSize": 1,
        "lotMin": 0.01, "lotMax": 100, "lotStep": 0.01, "tickSize": 0.1,
        "tickValue": 0.1, "precision": 1, "leverage": 5, "stopsLevel": 20,
    })
    assert spec.symbol == "US500"
    assert spec.money_per_price_unit_per_lot == pytest.approx(1.0)
    assert spec.point == pytest.approx(0.1)
    assert spec.min_stop_distance == pytest.approx(2.0)
    assert spec.round_price(5234.567) == pytest.approx(5234.6)


def test_symbol_spec_falls_back_to_digits_and_contract_size():
    spec = XtbBroker._to_symbol_spec({"symbol": "X", "digits": 3, "contractSize": 25})
    assert spec.precision == 3
    assert spec.tick_size == pytest.approx(0.001)
    # No tick value supplied: the contract size is the correct identity.
    assert spec.money_per_price_unit_per_lot == pytest.approx(25.0)


def test_volume_rounding_never_rounds_up():
    spec = XtbBroker._to_symbol_spec({"symbol": "X", "lotMin": 0.1, "lotStep": 0.1, "lotMax": 5})
    assert spec.round_volume(0.37) == pytest.approx(0.3)
    assert spec.round_volume(0.999) == pytest.approx(0.9)
    assert spec.normalise_volume(9.0) == pytest.approx(5.0)


def test_position_mapping_reads_the_direction():
    long_position = XtbBroker._to_position({
        "position": 42, "symbol": "US500", "cmd": CMD_BUY, "volume": 1.5,
        "open_price": 5000.0, "sl": 4980.0, "tp": 5050.0, "profit": 12.5,
        "open_time": 1_600_000_000_000,
    })
    assert long_position.direction is Direction.LONG
    assert long_position.position_id == 42
    assert long_position.stop_loss == 4980.0
    assert long_position.open_time == to_utc(1_600_000_000_000)

    short_position = XtbBroker._to_position({"position": 7, "cmd": CMD_SELL, "volume": 1})
    assert short_position.direction is Direction.SHORT
    # A zero SL from XTB means "no stop", not "stop at zero".
    assert short_position.stop_loss is None


def test_position_net_profit_includes_costs():
    position = XtbBroker._to_position({
        "position": 1, "cmd": CMD_BUY, "volume": 1, "profit": 100.0,
        "commission": -4.0, "storage": -1.5,
    })
    assert position.net_profit == pytest.approx(94.5)


def test_quote_sides():
    quote = Quote("EURUSD", bid=1.0849, ask=1.0851, timestamp=datetime.now(timezone.utc))
    assert quote.mid == pytest.approx(1.0850)
    assert quote.spread == pytest.approx(0.0002)
    # Buying pays the ask and exits on the bid.
    assert quote.price_for(Direction.LONG) == 1.0851
    assert quote.exit_price_for(Direction.LONG) == 1.0849
    assert quote.price_for(Direction.SHORT) == 1.0849
    assert quote.exit_price_for(Direction.SHORT) == 1.0851


def test_rate_limiter_spaces_calls():
    import time

    limiter = RateLimiter(0.05)
    start = time.monotonic()
    for _ in range(3):
        limiter.wait()
    # The first call is free, the next two wait one interval each.
    assert time.monotonic() - start >= 0.09
