"""Paper Trading: a virtual account driven by stored END-OF-DAY bars. No real orders exist.

Each `process_day` call, for one trading date D:
  1. fills pending BUY orders at D's open (market) or when D's low reaches the limit (limit);
     orders that cannot be filled are kept until they expire, then marked EXPIRED;
     orders that would exceed the cash are REJECTED;
  2. checks stops/targets inside D's bar (stop first when both are touched), time exits at D's close;
  3. evaluates the account's strategy at D's close and queues BUY orders for the next day.
The same fill rules as the backtest engine are used so paper and backtest results can be compared.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from app import db
from app.backtesting.engine import ExitConfig
from app.indicators import compute_indicators
from app.risk import CostModel, RiskConfig, position_size, r_target
from app.strategies import get_strategy


def create_account(name: str, initial_cash: float, strategy: str, strategy_params: dict | None = None,
                   exit_cfg: dict | None = None, risk_cfg: dict | None = None, costs: dict | None = None,
                   universe_market: str = "KR", start_date: str | None = None, db_path: Path | str | None = None) -> int:
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    get_strategy(strategy, **(strategy_params or {}))  # validates
    ExitConfig.from_dict(exit_cfg)
    params = {"strategy_params": strategy_params or {}, "exit": exit_cfg or {}, "risk": risk_cfg or {},
              "costs": costs or {}, "market": universe_market.upper()}
    return db.execute("""INSERT INTO paper_accounts (name, initial_cash, cash, strategy, params, created_at, last_processed_date, peak_equity)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (name, initial_cash, initial_cash, strategy, db.dumps(params), db.utc_now(), start_date, initial_cash), db_path)


def _account(account_id: int, db_path) -> dict:
    rows = db.fetch_all("SELECT * FROM paper_accounts WHERE id = ?", (account_id,), db_path)
    if not rows:
        raise KeyError(f"paper account {account_id} not found")
    acc = rows[0]
    acc["params"] = json.loads(acc["params"] or "{}")
    return acc


def place_order(account_id: int, market: str, symbol: str, qty: int, order_type: str = "market", limit_price: float | None = None,
                created_date: str | None = None, valid_days: int = 1, note: str = "manual", db_path=None) -> int:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if order_type not in {"market", "limit"} or (order_type == "limit" and not (limit_price and limit_price > 0)):
        raise ValueError("order_type must be market, or limit with a positive limit_price")
    acc = _account(account_id, db_path)
    created = created_date or acc["last_processed_date"] or pd.Timestamp.today().strftime("%Y-%m-%d")
    expires = (pd.Timestamp(created) + pd.tseries.offsets.BDay(max(1, valid_days))).strftime("%Y-%m-%d")
    return db.execute("""INSERT INTO paper_orders (account_id, market, symbol, side, qty, order_type, limit_price, status,
                         created_date, expires_date, note) VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'PENDING', ?, ?, ?)""",
                      (account_id, market.upper(), symbol.upper(), int(qty), order_type, limit_price, created, expires, note), db_path)


def cancel_order(account_id: int, order_id: int, db_path=None) -> bool:
    n = db.execute("UPDATE paper_orders SET status='CANCELLED' WHERE id=? AND account_id=? AND status='PENDING'", (order_id, account_id), db_path)
    return bool(n)


def _frames(market: str, db_path) -> dict[tuple[str, str], pd.DataFrame]:
    """Indicator frames over the full stored history. Indicators are causal, so slicing these at
    date D gives exactly what was known at D's close."""
    frame = db.load_candles(market=market, db_path=db_path)
    if frame.empty:
        return {}
    bench = set(r["symbol"] for r in db.fetch_all("SELECT symbol FROM instruments WHERE market=? AND is_benchmark=1", (market,), db_path))
    frame = frame[~frame["symbol"].isin(bench)]
    return {k: compute_indicators(g).reset_index(drop=True) for k, g in frame.groupby(["market", "symbol"])}


def process_day(account_id: int, day: str, db_path=None, full_frames: dict | None = None, signal_cache: dict | None = None) -> dict:
    acc = _account(account_id, db_path)
    p = acc["params"]
    D = pd.Timestamp(day)
    if acc["last_processed_date"] and D <= pd.Timestamp(acc["last_processed_date"]):
        return {"date": day, "skipped": "already processed"}
    exit_cfg = ExitConfig.from_dict(p.get("exit"))
    risk = RiskConfig.from_dict({"initial_cash": acc["initial_cash"], **p.get("risk", {})})
    costs = CostModel(**p["costs"]) if p.get("costs") else CostModel()
    full_frames = full_frames if full_frames is not None else _frames(p.get("market", "KR"), db_path)
    frames = {k: x[x["date"] <= D] for k, x in full_frames.items()}
    frames = {k: x for k, x in frames.items() if not x.empty}
    bars = {k: x.iloc[-1] for k, x in frames.items() if not x.empty and x.iloc[-1]["date"] == D}
    cash = float(acc["cash"])
    events: list[str] = []

    # 1. pending orders
    for o in db.fetch_all("SELECT * FROM paper_orders WHERE account_id=? AND status='PENDING' AND created_date < ? ORDER BY id",
                          (account_id, day), db_path):
        key = (o["market"], o["symbol"])
        bar = bars.get(key)
        held = db.fetch_all("SELECT 1 FROM paper_positions WHERE account_id=? AND market=? AND symbol=?", (account_id, *key), db_path)
        if bar is None:
            if o["expires_date"] and day >= o["expires_date"]:
                db.execute("UPDATE paper_orders SET status='EXPIRED', reason='no bar before expiry' WHERE id=?", (o["id"],), db_path)
                events.append(f"order {o['id']} expired (no bar)")
            continue
        if held:
            db.execute("UPDATE paper_orders SET status='REJECTED', reason='already holding' WHERE id=?", (o["id"],), db_path)
            continue
        if o["order_type"] == "limit":
            if float(bar["low"]) > o["limit_price"]:
                if o["expires_date"] and day >= o["expires_date"]:
                    db.execute("UPDATE paper_orders SET status='EXPIRED', reason='limit not reached' WHERE id=?", (o["id"],), db_path)
                    events.append(f"order {o['id']} expired unfilled")
                continue
            fill = min(float(bar["open"]), float(o["limit_price"]))
        else:
            fill = costs.buy_fill(float(bar["open"]))
        notional = fill * o["qty"]
        fee = costs.buy_cost(notional)
        if notional + fee > cash:
            db.execute("UPDATE paper_orders SET status='REJECTED', reason='insufficient cash' WHERE id=?", (o["id"],), db_path)
            events.append(f"order {o['id']} rejected: cash")
            continue
        x = frames[key]
        sig_rows = x[x["date"] < D]
        atr = float(sig_rows.iloc[-1]["atr_14"]) if len(sig_rows) and pd.notna(sig_rows.iloc[-1]["atr_14"]) else float("nan")
        stop = o["stop_price"] or (fill - exit_cfg.atr_mult * atr if math.isfinite(atr) else fill * (1 - exit_cfg.stop_pct))
        target = o["target_price"] or (r_target(fill, stop, exit_cfg.target_r) if exit_cfg.target_r and stop < fill else None)
        cash -= notional + fee
        db.execute("UPDATE paper_orders SET status='FILLED', filled_date=?, fill_price=? WHERE id=?", (day, fill, o["id"]), db_path)
        db.execute("""INSERT INTO paper_positions (account_id, market, symbol, qty, entry_price, entry_date, stop_price, target_price, initial_risk)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (account_id, *key, o["qty"], fill, day, stop, target, fill - stop), db_path)
        events.append(f"BUY {key[1]} x{o['qty']} @ {fill:.2f}")

    # 2. exits
    for pos in db.fetch_all("SELECT * FROM paper_positions WHERE account_id=?", (account_id,), db_path):
        key = (pos["market"], pos["symbol"])
        bar = bars.get(key)
        if bar is None:
            continue
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        entry_day = pos["entry_date"] == day
        stop, target = pos["stop_price"], pos["target_price"]
        exit_px, reason, market_order = None, None, True
        if not entry_day and o <= stop:
            exit_px, reason = o, "STOP_GAP"
        elif not entry_day and target and o >= target:
            exit_px, reason, market_order = o, "TARGET_GAP", False
        elif l <= stop and target and h >= target:
            exit_px, reason = stop, "STOP_BOTH_TOUCHED_CONSERVATIVE"
        elif l <= stop:
            exit_px, reason = stop, "STOP"
        elif target and h >= target:
            exit_px, reason, market_order = target, "TARGET", False
        else:
            held_days = int(((frames[key]["date"] >= pd.Timestamp(pos["entry_date"])) & (frames[key]["date"] <= D)).sum())
            if held_days >= exit_cfg.max_hold_days:
                exit_px, reason = c, "TIME"
        if exit_px is None:
            continue
        px = costs.sell_fill(exit_px, market_order)
        proceeds = px * pos["qty"]
        fee = costs.sell_cost(proceeds)
        buy_fee = costs.buy_cost(pos["entry_price"] * pos["qty"])
        pnl = proceeds - fee - pos["entry_price"] * pos["qty"] - buy_fee
        cash += proceeds - fee
        db.execute("""INSERT INTO paper_trades (account_id, market, symbol, qty, entry_date, entry_price, exit_date, exit_price, reason, pnl, costs)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (account_id, *key, pos["qty"], pos["entry_date"], pos["entry_price"], day, px, reason, pnl, fee + buy_fee), db_path)
        db.execute("DELETE FROM paper_positions WHERE account_id=? AND market=? AND symbol=?", (account_id, *key), db_path)
        events.append(f"SELL {key[1]} {reason} @ {px:.2f} pnl {pnl:,.0f}")

    # 3. mark to market, then new signals for tomorrow
    positions = db.fetch_all("SELECT * FROM paper_positions WHERE account_id=?", (account_id,), db_path)
    last_close = {k: float(x.iloc[-1]["close"]) for k, x in frames.items() if not x.empty}
    equity = cash + sum(q["qty"] * last_close.get((q["market"], q["symbol"]), q["entry_price"]) for q in positions)
    peak = max(float(acc["peak_equity"] or acc["initial_cash"]), equity)
    strategy = get_strategy(acc["strategy"], **p.get("strategy_params", {}))
    pending = {(r["market"], r["symbol"]) for r in db.fetch_all("SELECT market, symbol FROM paper_orders WHERE account_id=? AND status='PENDING'", (account_id,), db_path)}
    held = {(q["market"], q["symbol"]) for q in positions}
    slots = risk.max_positions - len(held) - len(pending)
    # Drawdown pause, same rule as the backtest engine: pause new entries for `drawdown_halt_days`, then
    # measure the drawdown from the equity at the end of the pause.
    next_day = D + pd.tseries.offsets.BDay(1)
    halt_until = pd.Timestamp(acc["halt_until"]) if acc.get("halt_until") else None
    if halt_until is not None and risk.drawdown_halt_days and next_day > halt_until:
        peak, halt_until = equity, None
    if halt_until is None and equity / peak - 1 <= -risk.max_drawdown_halt:
        halt_until = next_day + pd.tseries.offsets.BDay(risk.drawdown_halt_days) if risk.drawdown_halt_days else pd.Timestamp("2262-01-01")
    halted = halt_until is not None and next_day <= halt_until
    new_orders = 0
    if not halted:
        for key, x in sorted(frames.items()):
            if slots <= 0:
                break
            if key in held or key in pending or key not in bars:
                continue
            if signal_cache is not None:
                if key not in signal_cache:
                    signal_cache[key] = strategy.signals(full_frames[key])
                fired = bool(signal_cache[key].iloc[len(x) - 1])
            else:
                fired = bool(strategy.signals(x).iloc[-1])
            if not fired:
                continue
            r = x.iloc[-1]
            est_entry = float(r["close"])
            atr = r["atr_14"]
            if pd.isna(atr) or atr <= 0:
                continue
            stop = est_entry - exit_cfg.atr_mult * float(atr)
            qty = position_size(equity, risk.risk_per_trade, est_entry, stop, risk.lot_size, risk.max_position_pct, cash, costs)
            if qty < 1:
                continue
            place_order(account_id, key[0], key[1], qty, created_date=day, note=f"signal:{acc['strategy']}", db_path=db_path)
            slots -= 1
            new_orders += 1
    db.execute("UPDATE paper_accounts SET cash=?, last_processed_date=?, peak_equity=?, halt_until=? WHERE id=?",
               (cash, day, peak, halt_until.strftime("%Y-%m-%d") if halt_until is not None else None, account_id), db_path)
    db.execute("INSERT OR REPLACE INTO paper_equity (account_id, date, equity, cash, positions) VALUES (?, ?, ?, ?, ?)",
               (account_id, day, equity, cash, len(positions)), db_path)
    return {"date": day, "equity": equity, "cash": cash, "positions": len(positions), "new_orders": new_orders,
            "halted": halted, "events": events}


def run_until(account_id: int, end_date: str | None = None, db_path=None) -> dict:
    acc = _account(account_id, db_path)
    market = acc["params"].get("market", "KR")
    dates = [r["date"] for r in db.fetch_all("SELECT DISTINCT date FROM candles WHERE market=? ORDER BY date", (market,), db_path)]
    start = acc["last_processed_date"]
    todo = [d for d in dates if (start is None or d > start) and (end_date is None or d <= end_date)]
    full = _frames(market, db_path)
    cache: dict = {}
    results = [process_day(account_id, d, db_path, full, cache) for d in todo]
    return {"processed_days": len(results), "last": results[-1] if results else None,
            "events": [e for r in results for e in r.get("events", [])][-100:]}


def summary(account_id: int, db_path=None) -> dict:
    acc = _account(account_id, db_path)
    trades = db.fetch_all("SELECT * FROM paper_trades WHERE account_id=? ORDER BY id", (account_id,), db_path)
    eq = db.fetch_all("SELECT date, equity, cash, positions FROM paper_equity WHERE account_id=? ORDER BY date", (account_id,), db_path)
    positions = db.fetch_all("SELECT * FROM paper_positions WHERE account_id=?", (account_id,), db_path)
    orders = db.fetch_all("SELECT * FROM paper_orders WHERE account_id=? ORDER BY id DESC LIMIT 100", (account_id,), db_path)
    realized = sum(t["pnl"] for t in trades)
    e = pd.Series([r["equity"] for r in eq], dtype=float)
    mdd = float((e / e.cummax() - 1).min()) if len(e) else 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {"account": {k: v for k, v in acc.items()}, "realized_pnl": realized,
            "equity": eq[-1]["equity"] if eq else acc["initial_cash"], "max_drawdown": mdd,
            "trades": trades, "win_rate": wins / len(trades) if trades else None, "positions": positions,
            "pending_orders": [o for o in orders if o["status"] == "PENDING"], "orders": orders, "equity_curve": eq,
            "note": "종가(일봉) 기준 모의투자. 실시간 체결이 아니며 실제 주문은 존재하지 않음."}


def list_accounts(db_path=None) -> list[dict]:
    return db.fetch_all("SELECT id, name, initial_cash, cash, strategy, created_at, last_processed_date FROM paper_accounts ORDER BY id", (), db_path)
