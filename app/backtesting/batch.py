"""Vectorised version of `events.simulate_trade` for many signals at once (research scale).

Same rules as the scalar simulator (and the portfolio engine):
  entry = next open + slippage; stop from the signal bar; gap through the stop -> fill at the open;
  open above the target -> fill at the open; same bar touches stop and (partial) target -> STOP;
  partial take-profit moves the stop to breakeven; ATR trailing and MA exits act from the next bar;
  time exit at the close of the `max_hold_days`-th bar after the signal.
A trade whose data ends before it is closed is marked incomplete (excluded, never guessed).
`tests/test_research.py` checks that both simulators agree trade by trade.
"""
from __future__ import annotations

import numpy as np

from app.risk import CostModel

REASONS = np.array(["", "STOP_GAP", "TARGET_GAP", "STOP_BOTH_TOUCHED_CONSERVATIVE", "STOP", "STOP_TRAIL_OR_BE",
                    "TARGET", "TIME", "MA20_EXIT", "MA60_EXIT", "INCOMPLETE", "INVALID_STOP"])
R = {name: i for i, name in enumerate(REASONS)}


def initial_stop(cfg, fill: np.ndarray, atr: np.ndarray, swing: np.ndarray) -> np.ndarray:
    if cfg.stop_type == "atr":
        stop = fill - cfg.atr_mult * atr
    elif cfg.stop_type == "swing_low":
        stop = swing * (1 - cfg.swing_buffer_pct)
    else:
        stop = fill * (1 - cfg.stop_pct)
    stop = np.where(np.isfinite(stop) & (stop > 0) & (stop < fill), stop, np.nan)
    return stop


def simulate_batch(arr: dict[str, np.ndarray], sig_idx: np.ndarray, cfg, costs: CostModel,
                   ends: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """arr: columns (open, high, low, close, atr_14, swing_low_10, ma20, ma60) as float arrays — one instrument,
    or many instruments concatenated, in which case `ends[k]` is the exclusive end row of signal k's instrument.
    sig_idx: positional rows of the signals. Returns per-signal arrays."""
    sig_idx = np.asarray(sig_idx, dtype=np.int64)
    m = len(sig_idx)
    n = len(arr["open"])
    H = int(cfg.max_hold_days)
    out = {"net_return": np.full(m, np.nan), "r_multiple": np.full(m, np.nan), "reason": np.zeros(m, dtype=np.int64),
           "exit_offset": np.full(m, -1, dtype=np.int64), "fill": np.full(m, np.nan), "stop": np.full(m, np.nan),
           "partial_done": np.zeros(m, dtype=bool)}
    if m == 0:
        return out
    cols = sig_idx[:, None] + 1 + np.arange(H)[None, :]           # bar j = signal + 1 + k
    lim = np.full(m, n, dtype=np.int64) if ends is None else np.asarray(ends, dtype=np.int64)
    valid = cols < lim[:, None]
    cc = np.minimum(cols, n - 1)
    O, Hh, L, C = arr["open"][cc], arr["high"][cc], arr["low"][cc], arr["close"][cc]
    ATR = arr["atr_14"][cc]
    entry_ok = valid[:, 0]
    fill = np.where(entry_ok, O[:, 0] * (1 + costs.slippage_bps / 10_000), np.nan)
    stop = initial_stop(cfg, fill, arr["atr_14"][sig_idx], arr["swing_low_10"][sig_idx])
    risk = fill - stop
    has_target = cfg.target_r is not None
    target = fill + (cfg.target_r or 0) * risk if has_target else np.full(m, np.inf)
    has_partial = cfg.partial_fraction > 0
    ppx = fill + cfg.partial_r * risk if has_partial else np.full(m, np.inf)
    frac = cfg.partial_fraction
    ma = arr.get(cfg.ma_exit) if cfg.ma_exit else None
    MA = ma[cc] if ma is not None else None

    alive = np.isfinite(stop) & entry_ok
    out["reason"][~(np.isfinite(stop)) & entry_ok] = R["INVALID_STOP"]
    out["reason"][~entry_ok] = R["INCOMPLETE"]
    remaining = np.where(alive, 1.0, 0.0)
    proceeds = np.zeros(m)
    fees = np.where(alive, fill * costs.commission_bps / 10_000, 0.0)
    cur_stop = stop.copy()
    pdone = np.zeros(m, dtype=bool)
    highest = fill.copy()
    pending = np.zeros(m, dtype=bool)
    reason = out["reason"]
    exit_k = out["exit_offset"]
    sell_mkt = 1 - costs.slippage_bps / 10_000
    sell_fee = (costs.commission_bps + costs.sell_tax_bps) / 10_000

    def sell(mask: np.ndarray, qty: np.ndarray, ref: np.ndarray, market: bool) -> None:
        px = ref * (sell_mkt if market else 1.0)
        q = np.where(mask, qty, 0.0)
        with np.errstate(invalid="ignore"):  # inf target x 0 qty outside the mask
            val = np.where(mask, q * px, 0.0)
        np.add(proceeds, val, out=proceeds)
        np.add(fees, val * sell_fee, out=fees)
        np.subtract(remaining, q, out=remaining)

    def close(mask: np.ndarray, code: int, k: int) -> None:
        reason[mask] = code
        exit_k[mask] = k
        alive[mask] = False

    for k in range(H):
        v = valid[:, k]
        # data ended while the trade was open -> incomplete
        ended = alive & ~v
        if ended.any():
            reason[ended] = R["INCOMPLETE"]
            alive[ended] = False
        act = alive & v
        if not act.any():
            break
        o, h, l, c = O[:, k], Hh[:, k], L[:, k], C[:, k]
        # pending MA exit fills at this open
        pm = act & pending
        if pm.any():
            sell(pm, remaining, o, True)
            close(pm, R[f"{cfg.ma_exit.upper()}_EXIT"], k)
            act &= ~pm
        if k > 0:
            g1 = act & (o <= cur_stop)
            sell(g1, remaining, o, True)
            close(g1, R["STOP_GAP"], k)
            act &= ~g1
            g2 = act & (o >= target)
            sell(g2, remaining, o, False)
            close(g2, R["TARGET_GAP"], k)
            act &= ~g2
            if has_partial:
                g3 = act & ~pdone & (o >= ppx)
                sell(g3, remaining * frac, o, False)
                pdone |= g3
                cur_stop = np.where(g3, np.maximum(cur_stop, fill), cur_stop)
        hs = act & (l <= cur_stop)
        hp = act & ~pdone & (h >= ppx) if has_partial else np.zeros(m, dtype=bool)
        ht = act & (h >= target)
        both = hs & (hp | ht)
        sell(both, remaining, cur_stop, True)
        close(both, R["STOP_BOTH_TOUCHED_CONSERVATIVE"], k)
        so = hs & ~both
        sell(so, remaining, cur_stop, True)
        close(so & (cur_stop < fill), R["STOP"], k)
        close(so & ~(cur_stop < fill), R["STOP_TRAIL_OR_BE"], k)
        rest = act & ~hs
        part = rest & hp
        if part.any():
            sell(part, remaining * frac, ppx, False)
            pdone |= part
            cur_stop = np.where(part, np.maximum(cur_stop, fill), cur_stop)
        tg = rest & ht
        sell(tg, remaining, target, False)
        close(tg, R["TARGET"], k)
        still = rest & ~ht
        highest = np.where(still, np.maximum(highest, c), highest)
        if k == H - 1:
            sell(still, remaining, c, True)
            close(still, R["TIME"], k)
            break
        # data ends right after this bar -> scalar simulator leaves the trade open -> incomplete
        if cfg.trail_atr_mult:
            a = ATR[:, k]
            ok = still & np.isfinite(a)
            cur_stop = np.where(ok, np.maximum(cur_stop, highest - cfg.trail_atr_mult * a), cur_stop)
        if MA is not None:
            mv = MA[:, k]
            pending = pending | (still & np.isfinite(mv) & (c < mv))
    closed = np.isin(reason, [R[x] for x in REASONS[1:10]])
    net = np.where(closed, (proceeds - fees) / fill - 1, np.nan)
    out.update({"net_return": net, "r_multiple": np.where(closed, net * fill / risk, np.nan), "fill": fill, "stop": stop,
                "partial_done": pdone})
    return out
