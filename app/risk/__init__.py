"""Risk Management Engine: costs, stops, R-multiple targets, position sizing and portfolio limits.

All defaults are research assumptions to be edited by the user. None of them is a guarantee of
safety or profit.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from app.settings import DEFAULT_COMMISSION_BPS, DEFAULT_SELL_TAX_BPS, DEFAULT_SLIPPAGE_BPS


@dataclass(frozen=True)
class CostModel:
    """Per-side costs in basis points. Sell tax applies to the sell notional only."""
    commission_bps: float = DEFAULT_COMMISSION_BPS
    sell_tax_bps: float = DEFAULT_SELL_TAX_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS

    @classmethod
    def zero(cls) -> "CostModel":
        return cls(0.0, 0.0, 0.0)

    def buy_fill(self, price: float) -> float:
        """Market buy fills worse than the reference price by the slippage."""
        return price * (1 + self.slippage_bps / 10_000)

    def sell_fill(self, price: float, market_order: bool = True) -> float:
        """Market/stop sells fill below the reference; a resting limit (target) fills at its price."""
        return price * (1 - self.slippage_bps / 10_000) if market_order else price

    def buy_cost(self, notional: float) -> float:
        return notional * self.commission_bps / 10_000

    def sell_cost(self, notional: float) -> float:
        return notional * (self.commission_bps + self.sell_tax_bps) / 10_000

    def to_dict(self) -> dict:
        return asdict(self)


def atr_stop(entry: float, atr: float, multiple: float = 1.5) -> float:
    if not (entry > 0 and atr > 0 and multiple > 0):
        raise ValueError("entry, atr and multiple must be positive")
    return entry - multiple * atr


def percent_stop(entry: float, pct: float = 0.07) -> float:
    if not (entry > 0 and 0 < pct < 1):
        raise ValueError("entry must be positive and 0 < pct < 1")
    return entry * (1 - pct)


def swing_low_stop(entry: float, swing_low: float, buffer_pct: float = 0.0) -> float | None:
    """Stop just under the recent swing low. None when the swing low is not below the entry."""
    stop = swing_low * (1 - buffer_pct)
    return stop if 0 < stop < entry else None


def r_target(entry: float, stop: float, r_multiple: float) -> float:
    """Target = entry + R × (entry − stop). R is the initial per-share risk."""
    risk = entry - stop
    if risk <= 0:
        raise ValueError("stop must be below entry for a long position")
    return entry + r_multiple * risk


def position_size(equity: float, risk_per_trade: float, entry: float, stop: float, lot_size: int = 1,
                  max_position_pct: float = 1.0, available_cash: float | None = None,
                  cost_model: CostModel | None = None) -> int:
    """Shares so that a stop-out loses about `risk_per_trade × equity` (before gap risk).

    Capped by `max_position_pct × equity` notional and by the cash available (including the buy
    commission). Rounded DOWN to the lot size; 0 means the trade cannot be taken.
    """
    if equity <= 0 or entry <= 0 or stop >= entry or risk_per_trade <= 0:
        return 0
    per_share_risk = entry - stop
    qty = math.floor((equity * risk_per_trade) / per_share_risk)
    qty = min(qty, math.floor(equity * max_position_pct / entry))
    if available_cash is not None:
        unit_cost = entry * (1 + ((cost_model.commission_bps if cost_model else 0.0) / 10_000))
        qty = min(qty, math.floor(available_cash / unit_cost))
    qty -= qty % max(1, lot_size)
    return max(0, int(qty))


@dataclass
class RiskConfig:
    initial_cash: float = 100_000_000.0
    risk_per_trade: float = 0.005          # 0.5% of equity lost if the stop is hit (before gaps/costs)
    max_positions: int = 10
    max_position_pct: float = 0.20
    max_sector_positions: int = 3          # by instruments.sector; unknown sectors are not limited
    daily_loss_limit: float = 0.03         # no new entries the day after equity fell this much
    max_drawdown_halt: float = 0.25        # drawdown from the peak that pauses new entries
    drawdown_halt_days: int = 20           # pause length (business days); afterwards the peak is reset to current equity.
                                           # 0 = never resume (v0.2 behaviour: with no open positions equity cannot
                                           # recover, so the halt became permanent — found on real data in v0.3)
    pause_after_consecutive_losses: int = 0  # 0 = monitor only
    pause_days: int = 5
    lot_size: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RiskConfig":
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in (data or {}).items() if k in known})
        if cfg.initial_cash <= 0 or not (0 < cfg.risk_per_trade <= 0.1) or cfg.max_positions < 1:
            raise ValueError("initial_cash > 0, 0 < risk_per_trade <= 0.1, max_positions >= 1 required")
        if not (0 < cfg.max_position_pct <= 1):
            raise ValueError("0 < max_position_pct <= 1 required")
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
