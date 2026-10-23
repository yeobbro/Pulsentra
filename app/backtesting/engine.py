"""Event-driven, long-only, daily portfolio backtest.

Timing (no look-ahead):
  * indicators and the entry signal use bars <= t (close of t);
  * the order is filled at the OPEN of the instrument's next bar (t+1) plus slippage;
  * stops/targets are checked inside each later bar with OHLC only. When one bar touches both
    the stop and a target, the STOP is assumed first (daily bars cannot show the intraday order);
  * an open below the stop fills at that open (gap), an open above a target fills at that open;
  * trailing stops and moving-average exits are decided at the close and act from the next bar;
  * the time exit sells at the close of the last allowed holding day.
Cash, position count, sector count, lot size, commissions, sell tax and slippage are applied.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.indicators import compute_indicators
from app.risk import CostModel, RiskConfig, position_size, r_target, swing_low_stop
from app.strategies.base import Strategy

Key = tuple[str, str]


@dataclass
class ExitConfig:
    stop_type: str = "atr"            # atr | swing_low | percent
    atr_mult: float = 1.5
    stop_pct: float = 0.07
    swing_buffer_pct: float = 0.0
    target_r: float | None = 2.0      # None = no fixed target
    partial_fraction: float = 0.0     # >0: sell this fraction at partial_r, then trail the rest
    partial_r: float = 1.5
    trail_atr_mult: float | None = None
    ma_exit: str | None = None        # "ma20" | "ma60" | None — exit next open after a close below it
    max_hold_days: int = 10

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExitConfig":
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in (data or {}).items() if k in known})
        if cfg.stop_type not in {"atr", "swing_low", "percent"}:
            raise ValueError("stop_type must be atr, swing_low or percent")
        if cfg.atr_mult <= 0 or not (0 < cfg.stop_pct < 1) or cfg.max_hold_days < 1:
            raise ValueError("atr_mult > 0, 0 < stop_pct < 1, max_hold_days >= 1 required")
        if cfg.target_r is not None and cfg.target_r <= 0:
            raise ValueError("target_r must be positive or null")
        if not (0 <= cfg.partial_fraction < 1):
            raise ValueError("0 <= partial_fraction < 1 required")
        if cfg.partial_fraction > 0 and cfg.trail_atr_mult is None:
            cfg.trail_atr_mult = 2.0  # the remainder needs an exit rule; documented default
        if cfg.ma_exit not in {None, "ma20", "ma60"}:
            raise ValueError("ma_exit must be ma20, ma60 or null")
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Position:
    key: Key
    qty: int
    entry_price: float
    entry_date: pd.Timestamp
    signal_date: pd.Timestamp
    stop: float
    initial_stop: float
    target: float | None
    partial_price: float | None
    risk_per_share: float
    entry_cost: float
    sector: str | None
    initial_qty: int = 0
    highest_close: float = 0.0
    bars_held: int = 0
    partial_done: bool = False
    exit_next_open: str | None = None
    fills: list = field(default_factory=list)       # (date, qty, price, reason, cost)
    flags: set = field(default_factory=set)


def prepare(frame: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> dict[Key, pd.DataFrame]:
    out: dict[Key, pd.DataFrame] = {}
    for key, raw in frame.groupby(["market", "symbol"], sort=True):
        x = compute_indicators(raw, benchmark)
        if not x.empty:
            out[(str(key[0]), str(key[1]))] = x.reset_index(drop=True)
    return out


def run_portfolio(prepared: dict[Key, pd.DataFrame], strategy: Strategy, exit_cfg: ExitConfig, risk: RiskConfig,
                  costs: CostModel, signal_filter: Callable[[Key, pd.DataFrame], pd.Series] | None = None,
                  instruments: pd.DataFrame | None = None, start: str | None = None, end: str | None = None) -> dict:
    start_ts = pd.Timestamp(start) if start else None
    end_ts = pd.Timestamp(end) if end else None
    meta = {}
    if instruments is not None and not instruments.empty:
        for r in instruments.to_dict("records"):
            meta[(r["market"], r["symbol"])] = r

    # Column arrays (numpy) per instrument; dates as int64 nanoseconds.
    A: dict[Key, dict] = {}
    due: dict[int, list[tuple[Key, int]]] = {}
    all_dates: set[int] = set()
    s_ns = start_ts.value if start_ts is not None else None
    e_ns = end_ts.value if end_ts is not None else None
    n_signals = 0
    for key, x in prepared.items():
        sig = strategy.signals(x)
        if signal_filter is not None:
            sig = sig & signal_filter(key, x).reindex(x.index, fill_value=False).astype(bool)
        d = x["date"].to_numpy(dtype="datetime64[ns]").astype("int64")
        cols = {"d": d, "dl": d, "n": len(x)}
        for c in ("open", "high", "low", "close", "atr_14", "swing_low_10", "volume_ratio_20", "ma20", "ma60"):
            cols[c] = x[c].to_numpy(dtype=float) if c in x else np.full(len(x), np.nan)
        cols["flags"] = x["quality_flags"].fillna("").astype(str).to_numpy() if "quality_flags" in x else np.full(len(x), "", dtype=object)
        A[key] = cols
        in_win = np.ones(len(d), dtype=bool)
        if s_ns is not None:
            in_win &= d >= s_ns
        if e_ns is not None:
            in_win &= d <= e_ns
        all_dates.update(d[in_win].tolist())
        sig_arr = sig.to_numpy(dtype=bool)
        for i in np.flatnonzero(sig_arr[:-1] & in_win[:-1]):
            nd = int(d[i + 1])
            if e_ns is None or nd <= e_ns:
                n_signals += 1
                due.setdefault(nd, []).append((key, int(i)))
    timeline = sorted(all_dates)
    if not timeline:
        return {"trades": [], "equity": pd.DataFrame(columns=["date", "equity", "cash", "positions"]), "skipped": {}, "signals": 0}
    last_day = timeline[-1]

    def row_at(key: Key, day: int) -> int | None:
        d = A[key]["d"]
        i = int(np.searchsorted(d, day))
        return i if i < len(d) and d[i] == day else None

    cash = float(risk.initial_cash)
    positions: dict[Key, _Position] = {}
    last_close: dict[Key, float] = {}
    trades: list[dict] = []
    equity_rows: list[dict] = []
    skipped: dict[str, int] = {}
    peak = cash
    prev_equity = [cash, cash]
    consecutive_losses = 0
    pause_until: pd.Timestamp | None = None
    dd_pause_until: pd.Timestamp | None = None
    dd_triggers = 0
    halted_days = 0

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def close_out(pos: _Position, day: pd.Timestamp, qty: int, ref_price: float, reason: str, market_order: bool) -> None:
        nonlocal cash, consecutive_losses
        price = costs.sell_fill(ref_price, market_order)
        notional = price * qty
        fee = costs.sell_cost(notional)
        cash += notional - fee
        pos.fills.append((day, qty, price, reason, fee))
        pos.qty -= qty
        if pos.qty == 0:
            sold_qty = sum(f[1] for f in pos.fills)
            proceeds = sum(f[1] * f[2] - f[4] for f in pos.fills)
            cost_in = pos.entry_price * sold_qty + pos.entry_cost
            pnl = proceeds - cost_in
            exit_px = sum(f[1] * f[2] for f in pos.fills) / sold_qty
            reasons = [f[3] for f in pos.fills]
            trades.append({
                "market": pos.key[0], "symbol": pos.key[1],
                "signal_date": pos.signal_date.strftime("%Y-%m-%d"), "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                "exit_date": day.strftime("%Y-%m-%d"), "qty": sold_qty, "entry": round(pos.entry_price, 4),
                "initial_stop": round(pos.initial_stop, 4), "target": None if pos.target is None else round(pos.target, 4),
                "exit": round(exit_px, 4), "reason": "+".join(dict.fromkeys(reasons)), "final_reason": reasons[-1],
                "pnl": pnl, "costs": pos.entry_cost + sum(f[4] for f in pos.fills),
                "net_return": pnl / (pos.entry_price * sold_qty), "r_multiple": pnl / (pos.risk_per_share * sold_qty),
                "holding_days": pos.bars_held if pos.bars_held else 1, "partial": pos.partial_done,
                "quality_flags": sorted(pos.flags),
            })
            consecutive_losses = consecutive_losses + 1 if pnl <= 0 else 0
            del positions[pos.key]

    for day_ns in timeline:
        day = pd.Timestamp(day_ns)
        # ---- 1. risk gates for new entries (decided with information up to yesterday's close)
        equity_y = prev_equity[-1]
        block_reason = None
        if prev_equity[-2] > 0 and equity_y / prev_equity[-2] - 1 <= -risk.daily_loss_limit:
            block_reason = "blocked_daily_loss_limit"
        if dd_pause_until is not None and risk.drawdown_halt_days and day > dd_pause_until:
            peak, dd_pause_until = equity_y, None          # pause over: measure drawdown from here
        if dd_pause_until is None and peak > 0 and equity_y / peak - 1 <= -risk.max_drawdown_halt:
            dd_pause_until = day + pd.tseries.offsets.BDay(risk.drawdown_halt_days) if risk.drawdown_halt_days else pd.Timestamp.max
            dd_triggers += 1
        if dd_pause_until is not None and day <= dd_pause_until:
            block_reason = "blocked_drawdown_halt"
        if pause_until is not None and day <= pause_until:
            block_reason = "blocked_consecutive_losses"
        if block_reason:
            halted_days += 1

        rows_today = {key: row_at(key, day_ns) for key in positions}

        # ---- 2. open: pending market exits, then entries
        for key, pos in list(positions.items()):
            i = rows_today.get(key)
            if i is not None and pos.exit_next_open:
                close_out(pos, day, pos.qty, float(A[key]["open"][i]), pos.exit_next_open, True)

        candidates = due.pop(day_ns, [])

        def _rank(c: tuple[Key, int]) -> tuple[float, Key]:
            v = float(A[c[0]]["volume_ratio_20"][c[1]])
            return (-(v if v == v else 0.0), c[0])
        candidates.sort(key=_rank)  # deterministic priority when cash/slots are limited
        for key, sig_i in candidates:
            a = A[key]
            i = sig_i + 1
            if block_reason:
                skip(block_reason)
                continue
            if key in positions:
                skip("already_holding")
                continue
            if len(positions) >= risk.max_positions:
                skip("max_positions")
                continue
            m = meta.get(key, {})
            if m.get("listed_date") and day < pd.Timestamp(m["listed_date"]):
                skip("not_listed_yet")
                continue
            if m.get("delisted_date") and day >= pd.Timestamp(m["delisted_date"]):
                skip("delisted")
                continue
            sector = m.get("sector")
            if sector and sum(1 for p in positions.values() if p.sector == sector) >= risk.max_sector_positions:
                skip("sector_limit")
                continue
            fill = costs.buy_fill(float(a["open"][i]))
            atr = float(a["atr_14"][sig_i])
            if exit_cfg.stop_type == "atr":
                stop = fill - exit_cfg.atr_mult * atr if math.isfinite(atr) and atr > 0 else None
            elif exit_cfg.stop_type == "swing_low":
                sl = float(a["swing_low_10"][sig_i])
                stop = swing_low_stop(fill, sl, exit_cfg.swing_buffer_pct) if sl == sl else None
            else:
                stop = fill * (1 - exit_cfg.stop_pct)
            if stop is None or not (0 < stop < fill):
                skip("invalid_stop")
                continue
            equity_now = cash + sum(p.qty * last_close.get(k, p.entry_price) for k, p in positions.items())
            qty = position_size(equity_now, risk.risk_per_trade, fill, stop, risk.lot_size, risk.max_position_pct, cash, costs)
            if qty < 1:
                skip("size_zero_or_cash")
                continue
            notional = fill * qty
            fee = costs.buy_cost(notional)
            cash -= notional + fee
            target = r_target(fill, stop, exit_cfg.target_r) if exit_cfg.target_r else None
            partial = r_target(fill, stop, exit_cfg.partial_r) if exit_cfg.partial_fraction > 0 else None
            pos = _Position(key, qty, fill, day, pd.Timestamp(int(a["dl"][sig_i])), stop, stop, target, partial, fill - stop, fee,
                            sector, initial_qty=qty, highest_close=fill)
            for j in (sig_i, i):
                pos.flags.update(filter(None, a["flags"][j].split(",")))
            positions[key] = pos
            rows_today[key] = i

        # ---- 3. intrabar stops / targets
        for key, pos in list(positions.items()):
            i = rows_today.get(key)
            if i is None:
                continue
            a = A[key]
            o, h, l = float(a["open"][i]), float(a["high"][i]), float(a["low"][i])
            entry_day = pos.entry_date == day and pos.bars_held == 0
            if not entry_day:
                pos.flags.update(filter(None, a["flags"][i].split(",")))
                if o <= pos.stop:
                    close_out(pos, day, pos.qty, o, "STOP_GAP", True)
                    continue
                first_up = pos.partial_price if (pos.partial_price and not pos.partial_done) else pos.target
                if first_up is not None and o >= first_up:
                    if pos.partial_price and not pos.partial_done:
                        part = max(1, int(pos.qty * exit_cfg.partial_fraction))
                        if part < pos.qty:
                            close_out(pos, day, part, o, "PARTIAL_GAP", False)
                            pos.partial_done = True
                            pos.stop = max(pos.stop, pos.entry_price)
                    if pos.key in positions and pos.target is not None and o >= pos.target:
                        close_out(pos, day, pos.qty, o, "TARGET_GAP", False)
                        continue
            if pos.key not in positions:
                continue
            hit_stop = l <= pos.stop
            hit_partial = pos.partial_price is not None and not pos.partial_done and h >= pos.partial_price
            hit_target = pos.target is not None and h >= pos.target
            if hit_stop and (hit_partial or hit_target):
                close_out(pos, day, pos.qty, pos.stop, "STOP_BOTH_TOUCHED_CONSERVATIVE", True)
                continue
            if hit_stop:
                close_out(pos, day, pos.qty, pos.stop, "STOP" if pos.stop < pos.entry_price else "STOP_TRAIL_OR_BE", True)
                continue
            if hit_partial:
                part = max(1, int(pos.qty * exit_cfg.partial_fraction))
                if part < pos.qty:
                    close_out(pos, day, part, pos.partial_price, "PARTIAL_TARGET", False)
                    pos.partial_done = True
                    pos.stop = max(pos.stop, pos.entry_price)  # move the rest to breakeven
            if pos.key in positions and hit_target:
                close_out(pos, day, pos.qty, pos.target, "TARGET", False)

        # ---- 4. close: time / MA / trailing decisions, data end, mark to market
        for key, pos in list(positions.items()):
            i = rows_today.get(key)
            if i is None:
                continue
            a = A[key]
            c = float(a["close"][i])
            last_close[key] = c
            pos.bars_held += 1
            pos.highest_close = max(pos.highest_close, c)
            is_last_bar = i == a["n"] - 1 or (e_ns is not None and a["dl"][min(i + 1, a["n"] - 1)] > e_ns)
            if pos.bars_held >= exit_cfg.max_hold_days:
                close_out(pos, day, pos.qty, c, "TIME", True)
            elif is_last_bar:
                m = meta.get(key, {})
                delisted = bool(m.get("delisted_date"))
                close_out(pos, day, pos.qty, c, "DELISTED_LAST_CLOSE" if delisted else ("END_OF_TEST" if day_ns == last_day else "DATA_END"), True)
            else:
                if exit_cfg.trail_atr_mult:
                    atr_t = float(a["atr_14"][i])
                    if atr_t == atr_t:
                        pos.stop = max(pos.stop, pos.highest_close - exit_cfg.trail_atr_mult * atr_t)
                if exit_cfg.ma_exit:
                    mv = float(a[exit_cfg.ma_exit][i])
                    if mv == mv and c < mv:
                        pos.exit_next_open = f"{exit_cfg.ma_exit.upper()}_EXIT"

        equity = cash + sum(p.qty * last_close.get(k, p.entry_price) for k, p in positions.items())
        peak = max(peak, equity)
        prev_equity = [prev_equity[-1], equity]
        if risk.pause_after_consecutive_losses and consecutive_losses >= risk.pause_after_consecutive_losses:
            pause_until = day + pd.tseries.offsets.BDay(risk.pause_days)
            consecutive_losses = 0
        equity_rows.append({"date": day, "equity": equity, "cash": cash, "positions": len(positions)})

    eq = pd.DataFrame(equity_rows)
    eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1
    return {"trades": trades, "equity": eq, "skipped": skipped, "signals": n_signals, "halted_days": halted_days,
            "drawdown_halts": dd_triggers}
