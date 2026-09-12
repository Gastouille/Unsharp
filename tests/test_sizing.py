"""Progressive money management."""

from __future__ import annotations

import dataclasses

import pytest

from unsharp_bot.config import RiskConfig
from unsharp_bot.models import AccountState, Direction
from unsharp_bot.risk.sizing import ProgressivePositionSizer, SizingInputs


def account(equity: float, margin_used: float = 0.0) -> AccountState:
    return AccountState(
        balance=equity,
        equity=equity,
        margin_used=margin_used,
        margin_free=equity - margin_used,
        currency="EUR",
    )


def test_capital_available_subtracts_margin_and_buffer():
    sizer = ProgressivePositionSizer(RiskConfig())  # buffer 10%
    assert sizer.capital_available(account(10_000)) == pytest.approx(9_000.0)
    assert sizer.capital_available(account(10_000, margin_used=2_000)) == pytest.approx(7_000.0)
    # Never negative.
    assert sizer.capital_available(account(1_000, margin_used=5_000)) == 0.0


def test_sizing_is_progressive_with_equity(index_spec):
    """Twice the equity means twice the stake - and never more."""
    sizer = ProgressivePositionSizer(RiskConfig())
    small = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    large = sizer.size(SizingInputs(index_spec, account(20_000), Direction.LONG, 5234.5, 5220.0))
    assert small.accepted and large.accepted
    assert large.volume == pytest.approx(2 * small.volume, rel=1e-3)
    assert large.risk_amount == pytest.approx(2 * small.risk_amount, rel=1e-3)


def test_sizing_shrinks_when_equity_falls(index_spec):
    sizer = ProgressivePositionSizer(RiskConfig())
    before = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    after = sizer.size(SizingInputs(index_spec, account(7_000), Direction.LONG, 5234.5, 5220.0))
    assert after.volume < before.volume


def test_no_martingale_after_a_loss(index_spec):
    """A loss reduces equity, so the next stake is smaller - never doubled."""
    sizer = ProgressivePositionSizer(RiskConfig())
    first = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    # Simulate the loss actually taken.
    equity_after = 10_000 - first.risk_amount
    second = sizer.size(SizingInputs(index_spec, account(equity_after), Direction.LONG, 5234.5, 5220.0))
    assert second.volume < first.volume


def test_max_loss_constraint_binds(index_spec):
    """The loss at the stop never exceeds max_loss_fraction_of_equity."""
    config = dataclasses.replace(RiskConfig(), max_loss_fraction_of_equity=0.01)
    sizer = ProgressivePositionSizer(config)
    decision = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    assert decision.accepted
    assert decision.risk_amount <= 0.01 * 10_000 + 1e-6
    assert "capped_by_max_loss_constraint" in decision.notes


def test_capital_fraction_can_be_the_binding_constraint(index_spec):
    """With a wide stop and a small fraction, the capital rule bites first."""
    config = dataclasses.replace(
        RiskConfig(), risk_fraction_per_trade=0.05, max_loss_fraction_of_equity=0.5
    )
    sizer = ProgressivePositionSizer(config)
    decision = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    assert decision.accepted
    assert "capped_by_capital_fraction" in decision.notes


def test_risk_fraction_scales_the_notional(index_spec):
    """Halving risk_fraction_per_trade halves the committed notional."""
    wide = dataclasses.replace(
        RiskConfig(), risk_fraction_per_trade=0.5, max_loss_fraction_of_equity=0.9
    )
    narrow = dataclasses.replace(wide, risk_fraction_per_trade=0.25)
    inputs = SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0)
    big = ProgressivePositionSizer(wide).size(inputs)
    small = ProgressivePositionSizer(narrow).size(inputs)
    assert small.volume == pytest.approx(big.volume / 2, rel=1e-2)


def test_refuses_when_min_lot_breaches_the_loss_cap(index_spec):
    """Tiny account + big minimum lot => no trade at all."""
    spec = dataclasses.replace(index_spec, lot_min=5.0, lot_step=1.0)
    sizer = ProgressivePositionSizer(RiskConfig())
    decision = sizer.size(SizingInputs(spec, account(500), Direction.LONG, 5234.5, 5220.0))
    assert not decision.accepted
    assert decision.reason == "min_lot_exceeds_max_loss"


def test_refuses_on_a_zero_stop_distance(index_spec):
    sizer = ProgressivePositionSizer(RiskConfig())
    decision = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5234.5))
    assert not decision.accepted
    assert decision.reason == "zero_stop_distance"


def test_refuses_without_capital(index_spec):
    sizer = ProgressivePositionSizer(RiskConfig())
    decision = sizer.size(
        SizingInputs(index_spec, account(1_000, margin_used=1_000), Direction.LONG, 5234.5, 5220.0)
    )
    assert not decision.accepted
    assert decision.reason == "no_capital_available"


def test_volume_respects_the_lot_step(index_spec):
    spec = dataclasses.replace(index_spec, lot_step=0.5, lot_min=0.5)
    sizer = ProgressivePositionSizer(RiskConfig())
    decision = sizer.size(SizingInputs(spec, account(50_000), Direction.LONG, 5234.5, 5220.0))
    assert decision.accepted
    assert (decision.volume / 0.5) == pytest.approx(round(decision.volume / 0.5))


def test_broker_margin_calculator_is_used(index_spec):
    """When the broker gives an exact margin, it beats the local estimate."""
    calls: list[tuple[str, float]] = []

    def margin_calculator(symbol: str, volume: float) -> float:
        calls.append((symbol, volume))
        return 1234.0

    sizer = ProgressivePositionSizer(RiskConfig(), margin_calculator=margin_calculator)
    decision = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5234.5, 5220.0))
    assert decision.accepted
    assert calls and calls[0][0] == "US500"
    assert decision.margin_required == pytest.approx(1234.0)


def test_forex_sizing_uses_tick_value(forex_spec):
    """A 20-pip stop on EURUSD must cost exactly 20 pips x volume x pip value."""
    sizer = ProgressivePositionSizer(RiskConfig())
    entry, stop = 1.08500, 1.08300      # 20 pips = 200 pipettes
    decision = sizer.size(SizingInputs(forex_spec, account(10_000), Direction.LONG, entry, stop))
    assert decision.accepted
    # money_per_price_unit_per_lot = tick_value / tick_size = 1 / 0.00001 = 100_000
    expected = decision.volume * 0.002 * 100_000
    assert decision.risk_amount == pytest.approx(expected, rel=1e-6)
    assert decision.risk_amount <= 0.02 * 10_000 + 1e-6


def test_exposure_basis_engages_the_contract_value(index_spec):
    config = dataclasses.replace(
        RiskConfig(), notional_basis="exposure", max_loss_fraction_of_equity=0.9
    )
    sizer = ProgressivePositionSizer(config)
    decision = sizer.size(SizingInputs(index_spec, account(10_000), Direction.LONG, 5000.0, 4900.0))
    assert decision.accepted
    # 50% of 9_000 available capital => 4_500 of contract value at 5_000/lot.
    assert decision.volume == pytest.approx(0.9, abs=0.01)
