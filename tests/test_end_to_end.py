"""End-to-end: detection -> sizing -> order -> journal, through the paper broker."""

from __future__ import annotations

import dataclasses
import math
import random
from datetime import timedelta


from unsharp_bot.backtest.runner import Backtester
from unsharp_bot.broker.base import OrderRequest
from unsharp_bot.broker.paper import PaperBroker, PaperBrokerConfig
from unsharp_bot.config import BotConfig, LoggingConfig, SessionWindow
from unsharp_bot.journal import SignalJournal
from unsharp_bot.models import Candle, Direction, SignalStatus, TradePlan
from unsharp_bot.risk.sizing import ProgressivePositionSizer, SizingInputs
from unsharp_bot.strategy.indicators import atr
from unsharp_bot.strategy.levels import LevelDetector
from unsharp_bot.strategy.planner import TradePlanner
from unsharp_bot.strategy.unsharp import UnsharpDetector

from .conftest import CandleBuilder, long_setup_series, short_setup_series


def base_config() -> BotConfig:
    """A config usable offline: paper broker, 24/7 session, no timing filter."""
    config = BotConfig()
    config.broker.name = "paper"
    config.sessions = [
        SessionWindow(
            name="always", start="00:00", end="23:59", timezone="UTC",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
    ]
    config.timing.enabled = False
    config.market.timeframe_minutes = 5
    config.validate()
    return config


def test_full_pipeline_long(tmp_path, index_spec):
    """A textbook setup goes all the way to an open position and a journal entry."""
    config = base_config()
    spec = dataclasses.replace(
        index_spec, symbol="TEST", precision=2, tick_size=0.01, tick_value=0.01,
        contract_size=1.0, leverage=5.0,
    )
    candles = long_setup_series()
    atr_value = atr(candles, config.unsharp.atr_period)
    levels = LevelDetector(config.levels).detect(candles, atr_value)

    result = UnsharpDetector(config.unsharp).detect("TEST", candles, levels, atr_value)
    assert result.found
    setup = result.setup

    geometry, why = TradePlanner(config.unsharp).build(setup, levels, spec)
    assert geometry is not None, why

    broker = PaperBroker({"TEST": spec}, PaperBrokerConfig(starting_balance=10_000))
    broker.connect()
    for candle in candles:
        broker.feed_candle("TEST", candle)

    account = broker.get_account_state()
    sizer = ProgressivePositionSizer(config.risk, broker.get_margin_for)
    decision = sizer.size(
        SizingInputs(spec, account, setup.direction, geometry.entry_price, geometry.stop_price)
    )
    assert decision.accepted
    # The hard risk cap is respected.
    assert decision.risk_amount <= config.risk.max_loss_fraction_of_equity * account.equity + 1e-6

    journal = SignalJournal(LoggingConfig(), tmp_path, 5)
    record = journal.record_detection(setup)
    plan = TradePlan(
        setup=setup,
        entry_price=geometry.entry_price,
        stop_price=geometry.stop_price,
        target_price=geometry.target_price,
        volume=decision.volume,
        risk_reward=geometry.risk_reward,
        risk_amount=decision.risk_amount,
        notional_engaged=decision.notional_engaged,
        capital_available=decision.capital_available,
        risk_fraction_used=config.risk.risk_fraction_per_trade,
        margin_required=decision.margin_required,
    )
    journal.attach_plan(record, plan)

    order = broker.open_trade(
        OrderRequest("TEST", setup.direction, plan.volume, plan.stop_price, plan.target_price)
    )
    assert order.accepted
    journal.update_status(record, SignalStatus.OPEN, position_id=order.position_id)
    assert len(broker.get_trades()) == 1
    assert journal.find_by_position(order.position_id) is not None

    # Drive price to the target: the position must close in profit.
    last = candles[-1]
    winner = Candle(
        last.timestamp + timedelta(minutes=5),
        open=last.close,
        high=plan.target_price + 0.5,
        low=last.close - 0.1,
        close=plan.target_price + 0.3,
    )
    fills = broker.feed_candle("TEST", winner)
    assert len(fills) == 1
    assert fills[0].reason == "take_profit"
    assert fills[0].profit > 0
    journal.update_status(
        record, SignalStatus.CLOSED, reason=fills[0].reason, pnl=fills[0].profit
    )
    assert journal.daily_summary()["wins"] == 1


def test_full_pipeline_short_hits_the_stop(index_spec):
    """The short path works symmetrically, and a stop loses exactly one R."""
    config = base_config()
    spec = dataclasses.replace(
        index_spec, symbol="TEST", precision=2, tick_size=0.01, tick_value=0.01
    )
    candles = short_setup_series()
    atr_value = atr(candles, config.unsharp.atr_period)
    levels = LevelDetector(config.levels).detect(candles, atr_value)
    result = UnsharpDetector(config.unsharp).detect("TEST", candles, levels, atr_value)
    assert result.found and result.setup.direction is Direction.SHORT

    geometry, why = TradePlanner(config.unsharp).build(result.setup, levels, spec)
    assert geometry is not None, why
    assert geometry.stop_price > geometry.entry_price > geometry.target_price

    broker = PaperBroker({"TEST": spec}, PaperBrokerConfig(starting_balance=10_000))
    broker.connect()
    for candle in candles:
        broker.feed_candle("TEST", candle)

    order = broker.open_trade(
        OrderRequest("TEST", Direction.SHORT, 1.0, geometry.stop_price, geometry.target_price)
    )
    assert order.accepted

    last = candles[-1]
    loser = Candle(
        last.timestamp + timedelta(minutes=5),
        open=last.close,
        high=geometry.stop_price + 0.5,
        low=last.close - 0.1,
        close=geometry.stop_price + 0.4,
    )
    fills = broker.feed_candle("TEST", loser)
    assert len(fills) == 1 and fills[0].reason == "stop_loss"
    assert fills[0].profit < 0


def test_paper_broker_assumes_the_stop_first_on_an_ambiguous_bar(index_spec):
    spec = dataclasses.replace(index_spec, symbol="TEST", precision=2)
    broker = PaperBroker({"TEST": spec}, PaperBrokerConfig(starting_balance=10_000))
    broker.connect()
    builder = CandleBuilder()
    broker.feed_candle("TEST", builder.add(100, 100.5, 99.5, 100).build()[0])
    broker.open_trade(OrderRequest("TEST", Direction.LONG, 1.0, 99.0, 101.0))
    # This bar touches both the stop and the target.
    ambiguous = Candle(builder.build()[0].timestamp + timedelta(minutes=5), 100, 101.5, 98.5, 100)
    fills = broker.feed_candle("TEST", ambiguous)
    assert len(fills) == 1 and fills[0].reason == "stop_loss"


def synthetic_market(count: int = 3000, seed: int = 7) -> list[Candle]:
    """A random walk with mean reversion: enough structure to trigger setups."""
    rng = random.Random(seed)
    builder = CandleBuilder(period_minutes=5)
    price = 5000.0
    for index in range(count):
        # Mean reversion towards a slow sine, plus noise and occasional shocks.
        anchor = 5000.0 + 60.0 * math.sin(index / 90.0)
        drift = (anchor - price) * 0.04
        shock = rng.gauss(0, 4.0) + (rng.gauss(0, 18.0) if rng.random() < 0.03 else 0.0)
        open_ = price
        close = price + drift + shock
        high = max(open_, close) + abs(rng.gauss(0, 2.5))
        low = min(open_, close) - abs(rng.gauss(0, 2.5))
        builder.add(open_, high, low, close, volume=rng.randint(50, 500))
        price = close
    return builder.build()


def test_backtester_runs_and_reports(index_spec):
    """The backtester must complete and produce internally consistent statistics."""
    config = base_config()
    spec = dataclasses.replace(index_spec, symbol="TEST")
    candles = synthetic_market()

    backtester = Backtester(config, starting_equity=10_000.0, apply_session_filter=False)
    report = backtester.run("TEST", candles, spec)

    assert report.signals_detected > 0, "the detector never fired on 3000 candles"
    assert len(report.equity_curve) == len(candles)
    summary = report.summary()
    assert summary["trades"] == len(report.closed_trades)
    assert 0.0 <= report.win_rate <= 1.0
    assert 0.0 <= report.max_drawdown <= 1.0
    # Every trade must respect the direction-consistent geometry.
    for trade in report.trades:
        if trade.direction is Direction.LONG:
            assert trade.stop_price < trade.entry_price < trade.target_price
        else:
            assert trade.stop_price > trade.entry_price > trade.target_price
    assert report.format_text().count("UNSHARP BACKTEST REPORT") == 1


def test_backtest_respects_the_per_trade_loss_cap(index_spec):
    """No simulated loss may exceed the configured max loss, plus one bar of gap."""
    config = base_config()
    config.risk.max_loss_fraction_of_equity = 0.02
    spec = dataclasses.replace(index_spec, symbol="TEST")
    report = Backtester(config, starting_equity=10_000.0, apply_session_filter=False).run(
        "TEST", synthetic_market(), spec
    )
    for trade in report.closed_trades:
        if trade.reason != "stop_loss" or trade.equity_after is None:
            continue
        # The stop fills at the stop price in the simulation, so the loss is exact.
        equity_before = trade.equity_after - (trade.pnl or 0.0)
        assert abs(trade.pnl or 0.0) <= 0.02 * equity_before * 1.05


def test_backtest_sizing_grows_with_equity(index_spec):
    """A larger account trades proportionally larger volumes."""
    config = base_config()
    spec = dataclasses.replace(index_spec, symbol="TEST")
    candles = synthetic_market()
    small = Backtester(config, starting_equity=10_000.0, apply_session_filter=False).run(
        "TEST", candles, spec
    )
    large = Backtester(config, starting_equity=100_000.0, apply_session_filter=False).run(
        "TEST", candles, spec
    )
    assert small.trades and large.trades
    assert large.trades[0].volume > small.trades[0].volume
