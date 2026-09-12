"""Progressive position sizing.

The sizing rule requested by the method is a **fixed fraction of the currently
available capital**, which is progressive by construction: the stake grows when
equity grows and shrinks when it falls.  There is no martingale anywhere - a
loss never increases the next stake.

    capital_available = equity - margin_used - buffer_fraction * equity
    notional          = capital_available * risk_fraction_per_trade

``notional`` is then converted into a volume (lots).  Two readings of
"notional" are supported, selected by ``risk.notional_basis``:

``margin`` (default)
    The money committed is the **margin** posted for the position.  Volume is
    derived from the instrument leverage (or from the broker's own margin
    calculator when available).  This is the conservative reading: you never
    commit more cash than the configured fraction.

``exposure``
    The money committed is the **contract value** (price x contract size x
    volume).  With leverage this uses far less cash than ``margin`` mode.

Whatever the basis, the result is then capped by the hard risk constraint:

    loss_if_stopped <= max_loss_fraction_of_equity * equity

The smaller of the two volumes wins, the result is floored to the broker lot
step, and the trade is refused outright when even the minimum lot would breach
the loss cap.  That ordering is the whole point: the *fraction of capital* sets
the ambition, the *max loss* sets the law.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from ..config import RiskConfig
from ..models import AccountState, Direction, SizingDecision, SymbolSpec

#: Signature of an optional broker callback returning the exact margin
#: required for ``(symbol, volume)``.  Used when ``use_broker_margin_check``.
MarginCalculator = Callable[[str, float], float | None]


@dataclass(slots=True)
class SizingInputs:
    """Everything the sizer needs for one decision."""

    symbol_spec: SymbolSpec
    account: AccountState
    direction: Direction
    entry_price: float
    stop_price: float


class ProgressivePositionSizer:
    """Fixed-fraction-of-available-capital sizing with hard risk caps."""

    def __init__(
        self,
        config: RiskConfig,
        margin_calculator: MarginCalculator | None = None,
    ) -> None:
        self.config = config
        self.margin_calculator = margin_calculator

    # ------------------------------------------------------------------ #
    # Capital
    # ------------------------------------------------------------------ #
    def capital_available(self, account: AccountState) -> float:
        """``equity - margin_used - buffer``, floored at zero.

        The buffer is a fraction of equity kept untouched so a normal drawdown
        never walks the account into a margin call.
        """
        buffer = self.config.buffer_fraction * account.equity
        return max(0.0, account.equity - account.margin_used - buffer)

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    def size(self, inputs: SizingInputs) -> SizingDecision:
        cfg = self.config
        spec = inputs.symbol_spec
        account = inputs.account
        notes: list[str] = []

        risk_per_unit = abs(inputs.entry_price - inputs.stop_price)
        if risk_per_unit <= 0:
            return SizingDecision(False, reason="zero_stop_distance")
        if account.equity <= 0:
            return SizingDecision(False, reason="non_positive_equity")

        capital = self.capital_available(account)
        if capital <= 0:
            return SizingDecision(
                False,
                reason="no_capital_available",
                capital_available=capital,
            )

        # --- 1. Ambition: a fixed fraction of the available capital -------- #
        notional_target = capital * cfg.risk_fraction_per_trade
        volume_from_notional = self._volume_for_notional(
            notional_target, inputs.entry_price, spec
        )
        if volume_from_notional <= 0:
            return SizingDecision(
                False,
                reason="cannot_convert_notional_to_volume",
                capital_available=capital,
            )

        # --- 2. Law: never risk more than max_loss_fraction of equity ------ #
        money_per_unit_per_lot = self._money_per_unit_per_lot(spec, inputs.entry_price)
        if money_per_unit_per_lot <= 0:
            return SizingDecision(
                False,
                reason="unknown_instrument_tick_value",
                capital_available=capital,
            )
        risk_per_lot = risk_per_unit * money_per_unit_per_lot
        max_loss = cfg.max_loss_fraction_of_equity * account.equity
        volume_from_risk = max_loss / risk_per_lot

        # --- 3. The binding constraint wins -------------------------------- #
        raw_volume = min(volume_from_notional, volume_from_risk)
        if volume_from_risk < volume_from_notional:
            notes.append("capped_by_max_loss_constraint")
        else:
            notes.append("capped_by_capital_fraction")

        volume = spec.normalise_volume(raw_volume)

        if volume < spec.lot_min:
            # Even the smallest tradable lot is too big for the risk budget:
            # check whether the minimum lot would still respect the loss cap.
            min_lot_loss = spec.lot_min * risk_per_lot
            if min_lot_loss > max_loss:
                return SizingDecision(
                    False,
                    reason="min_lot_exceeds_max_loss",
                    capital_available=capital,
                    risk_amount=min_lot_loss,
                    notes=notes + [
                        f"min_lot={spec.lot_min} would risk {min_lot_loss:.2f} "
                        f"> max_loss={max_loss:.2f}"
                    ],
                )
            volume = spec.lot_min
            notes.append("rounded_up_to_min_lot")

        if volume > spec.lot_max:
            volume = spec.normalise_volume(spec.lot_max)
            notes.append("capped_by_broker_lot_max")

        risk_amount = volume * risk_per_lot
        if risk_amount > max_loss * (1.0 + 1e-9):
            return SizingDecision(
                False,
                reason="risk_exceeds_max_loss_after_rounding",
                volume=volume,
                capital_available=capital,
                risk_amount=risk_amount,
                notes=notes,
            )

        # --- 4. Margin sanity check ---------------------------------------- #
        margin_required = self._margin_required(spec, volume, inputs.entry_price)
        margin_budget = capital * cfg.max_margin_fraction_of_capital
        if margin_required > margin_budget and margin_required > 0:
            scaled = volume * (margin_budget / margin_required)
            reduced = spec.normalise_volume(scaled)
            if reduced < spec.lot_min:
                return SizingDecision(
                    False,
                    reason="insufficient_margin_for_min_lot",
                    capital_available=capital,
                    margin_required=margin_required,
                    notes=notes,
                )
            notes.append("reduced_to_fit_available_margin")
            volume = reduced
            risk_amount = volume * risk_per_lot
            margin_required = self._margin_required(spec, volume, inputs.entry_price)

        notional_engaged = (
            margin_required
            if cfg.notional_basis == "margin" and margin_required > 0
            else self._exposure(volume, inputs.entry_price, spec)
        )

        return SizingDecision(
            accepted=True,
            volume=volume,
            reason="ok",
            risk_amount=risk_amount,
            notional_engaged=notional_engaged,
            capital_available=capital,
            margin_required=margin_required,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Conversions
    # ------------------------------------------------------------------ #
    def _volume_for_notional(
        self, notional: float, price: float, spec: SymbolSpec
    ) -> float:
        """Lots whose committed amount matches ``notional``."""
        if notional <= 0:
            return 0.0
        exposure_per_lot = self._exposure(1.0, price, spec)
        if exposure_per_lot <= 0:
            return 0.0
        if self.config.notional_basis == "exposure":
            return notional / exposure_per_lot
        # "margin" basis: exposure / leverage is the cash actually posted.
        margin_per_lot = self._margin_per_lot(spec, price)
        if margin_per_lot <= 0:
            return 0.0
        return notional / margin_per_lot

    @staticmethod
    def _exposure(volume: float, price: float, spec: SymbolSpec) -> float:
        """Contract value of ``volume`` lots at ``price``.

        Note: expressed in the instrument's quote currency.  For accounts in a
        different currency this is an approximation used only for budgeting;
        the authoritative numbers are the broker margin check and the loss cap,
        which are both computed from the instrument tick value.
        """
        return abs(volume) * abs(price) * (spec.contract_size or 1.0)

    @staticmethod
    def _leverage_ratio(spec: SymbolSpec) -> float:
        """Normalise XTB's percentage leverage into a ratio (e.g. 1% -> 100x)."""
        leverage = spec.leverage or 0.0
        if leverage <= 0:
            return 1.0
        # XTB reports "leverage" as a margin percentage (1 => 1% => 100:1).
        return 100.0 / leverage if leverage <= 100 else leverage

    def _margin_per_lot(self, spec: SymbolSpec, price: float) -> float:
        return self._exposure(1.0, price, spec) / self._leverage_ratio(spec)

    def _margin_required(self, spec: SymbolSpec, volume: float, price: float) -> float:
        """Exact margin from the broker when possible, local estimate otherwise."""
        if self.config.use_broker_margin_check and self.margin_calculator is not None:
            try:
                margin = self.margin_calculator(spec.symbol, volume)
            except Exception:  # pragma: no cover - broker hiccup must not block sizing
                margin = None
            if margin is not None and margin > 0 and math.isfinite(margin):
                return float(margin)
        return self._margin_per_lot(spec, price) * volume

    @staticmethod
    def _money_per_unit_per_lot(spec: SymbolSpec, price: float) -> float:
        """Money gained/lost per 1.0 of price movement, per lot."""
        return spec.money_per_price_unit_per_lot
