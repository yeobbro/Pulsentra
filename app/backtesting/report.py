"""Backtest reports: strategy vs costs-free run, benchmark, random-entry baseline, regimes,
monthly/yearly returns, walk-forward parameter selection and exit-rule comparison."""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from app.backtesting.engine import ExitConfig, prepare, run_portfolio
from app.backtesting.events import forward_returns, signal_outcomes, summarize
from app.backtesting.metrics import (buy_and_hold_curve, equal_weight_curve, equity_metrics, period_returns,
                                     trade_metrics)
from app.data.schema import is_demo_source
from app.risk import CostModel, RiskConfig
from app.strategies import get_strategy
from app.validation import param_grid, regime_series, time_folds

ASSUMPTIONS = [
    "신호는 t일 종가까지의 데이터로 계산하고, 진입은 다음 거래일 시가 + 슬리피지로 체결한다.",
    "같은 일봉에서 손절가와 익절가를 모두 지나면 손절이 먼저 일어난 것으로 처리한다(일봉으로 순서를 알 수 없음, 보수적 가정).",
    "시가가 손절가 아래면 시가에 손절(갭), 익절가 위면 시가에 익절한다.",
    "추적 손절·이동평균 청산은 종가로 판단하고 다음 봉부터 적용한다. 시간 청산은 마지막 보유일 종가.",
    "수수료·매도세·슬리피지는 입력한 가정값이며 실제 증권사·세율과 다를 수 있다.",
    "분할·병합·배당 반영 여부는 데이터 출처에 따른다. 반영되지 않은 시계열은 품질 경고로 표시된다.",
    "상장폐지 종목이 데이터에 없으면 생존편향이 생긴다. 종목 메타데이터(상장·폐지일) 적재 여부를 함께 확인할 것.",
]


def _downsample(df: pd.DataFrame, max_points: int = 600) -> list[dict]:
    if df.empty:
        return []
    step = max(1, int(np.ceil(len(df) / max_points)))
    d = df.iloc[::step].copy()
    if d.index[-1] != df.index[-1]:
        d = pd.concat([d, df.iloc[[-1]]])
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    return d.to_dict("records")


def data_info(frame: pd.DataFrame, instruments: pd.DataFrame | None) -> dict:
    sources = sorted(frame["source"].dropna().unique().tolist()) if "source" in frame else []
    synthetic = bool(sources) and all(is_demo_source(s) for s in sources)
    mixed_demo = any(is_demo_source(s) for s in sources) and not synthetic
    n_sym = frame.groupby(["market", "symbol"]).ngroups if not frame.empty else 0
    covered = 0
    delisted = 0
    if instruments is not None and not instruments.empty and n_sym:
        keys = set(map(tuple, frame[["market", "symbol"]].drop_duplicates().to_numpy()))
        inst = instruments[[tuple(k) in keys for k in instruments[["market", "symbol"]].to_numpy()]]
        covered = int(inst["listed_date"].notna().sum())
        delisted = int(inst["delisted_date"].notna().sum())
    flagged = int((frame["quality_flags"].fillna("") != "").sum()) if "quality_flags" in frame else 0
    adjusted = frame["adjusted"].value_counts(dropna=False).to_dict() if "adjusted" in frame else {}
    return {"sources": sources, "synthetic": synthetic, "mixed_demo_and_real": mixed_demo, "instruments": n_sym,
            "bars": int(len(frame)), "flagged_bars_in_input": flagged,
            "adjusted_counts": {str(k): int(v) for k, v in adjusted.items()},
            "universe_metadata": {"with_listing_dates": covered, "delisted_in_universe": delisted,
                                  "note": ("상장·폐지일 메타데이터가 없는 종목이 있으면 생존편향 여부를 확인할 수 없음"
                                           if covered < n_sym else "모든 종목에 상장일 정보 있음")},
            "warning": ("합성 데모 데이터 결과입니다. 실제 시장 성과를 의미하지 않습니다." if synthetic else
                        "데모와 실제 데이터가 섞여 있습니다. 결과를 해석하지 마세요." if mixed_demo else None)}


def _regime_table(trades: list[dict], index_curve: pd.DataFrame) -> dict:
    if not trades or index_curve.empty:
        return {}
    idx = index_curve.set_index(pd.to_datetime(index_curve["date"]))["equity"]
    reg = regime_series(idx)
    out: dict[str, Any] = {}
    t = pd.DataFrame(trades)
    t["regime"] = [reg.get(pd.Timestamp(d), "unknown") for d in t["signal_date"]]
    for name, g in t.groupby("regime"):
        m = trade_metrics(g.to_dict("records"))
        out[name] = {k: m[k] for k in ("trades", "win_rate", "win_rate_ci95", "expectancy_return", "expectancy_r", "profit_factor", "sample_warning")}
    out["_definition"] = "지수(또는 동일가중 대용 지수) 60거래일 수익률 > +5% 상승장, < −5% 하락장, 그 사이 횡보장. 신호일 기준."
    return out


def run_report(frame: pd.DataFrame, strategy_key: str, strategy_params: dict | None = None,
               exit_cfg: ExitConfig | None = None, risk: RiskConfig | None = None, costs: CostModel | None = None,
               benchmark: pd.DataFrame | None = None, instruments: pd.DataFrame | None = None,
               start: str | None = None, end: str | None = None, random_runs: int = 10,
               signal_filter: Callable | None = None, prepared: dict | None = None, keep_full: bool = False) -> dict:
    exit_cfg = exit_cfg or ExitConfig()
    risk = risk or RiskConfig()
    costs = costs or CostModel()
    strategy = get_strategy(strategy_key, **(strategy_params or {}))
    if not strategy.available:
        raise ValueError(f"{strategy.name}: {strategy.unavailable_reason}")
    prepared = prepared if prepared is not None else prepare(frame, benchmark)
    notes = []
    if "benchmark" in strategy.requires and benchmark is None:
        notes.append("이 전략은 벤치마크(시장지수) 데이터가 필요합니다. 적재되지 않아 상대강도 조건이 모두 '미확인' → 신호 없음.")

    net = run_portfolio(prepared, strategy, exit_cfg, risk, costs, signal_filter, instruments, start, end)
    gross = run_portfolio(prepared, strategy, exit_cfg, risk, CostModel.zero(), signal_filter, instruments, start, end)
    eq = net["equity"]
    em, tm = equity_metrics(eq, risk.initial_cash), trade_metrics(net["trades"])
    gross_em = equity_metrics(gross["equity"], risk.initial_cash)

    dates = eq["date"] if not eq.empty else pd.Series(dtype="datetime64[ns]")
    if benchmark is not None and not benchmark.empty:
        bench_curve, bench_label = buy_and_hold_curve(benchmark, dates, risk.initial_cash), "시장지수 매수 후 보유 (비용 미반영)"
    else:
        bench_curve, bench_label = equal_weight_curve(prepared, dates, risk.initial_cash), "검증 종목 동일가중 매수 후 보유 (지수 미적재 대용, 비용 미반영)"
    bench_m = equity_metrics(bench_curve, risk.initial_cash) if not bench_curve.empty else {}

    random_dist = None
    eligible = sum(int(x["atr_14"].notna().sum()) for x in prepared.values())
    if random_runs > 0 and eligible and net["signals"]:
        p = max(1e-4, min(0.5, net["signals"] / eligible))
        totals, expect = [], []
        for seed in range(1, random_runs + 1):
            rnd = run_portfolio(prepared, get_strategy("random_entry", probability=p, seed=seed), exit_cfg, risk, costs, None, instruments, start, end)
            totals.append(equity_metrics(rnd["equity"], risk.initial_cash)["total_return"])
            m = trade_metrics(rnd["trades"])
            if m["expectancy_r"] is not None:
                expect.append(m["expectancy_r"])
        tr = np.array(totals)
        random_dist = {"runs": random_runs, "entry_probability": p,
                       "total_return_mean": float(tr.mean()), "total_return_p05": float(np.quantile(tr, 0.05)),
                       "total_return_p95": float(np.quantile(tr, 0.95)),
                       "strategy_percentile": float((tr < em["total_return"]).mean()),
                       "expectancy_r_mean": float(np.mean(expect)) if expect else None,
                       "note": "같은 진입 빈도·청산·비용 규칙으로 무작위 진입한 결과 분포. 전략이 상위에 있지 않으면 우위가 확인되지 않은 것."}

    index_curve = bench_curve
    full = {"_equity_full": eq, "_gross_equity_full": gross["equity"], "_trades_full": net["trades"],
            "_gross_trades_full": gross["trades"], "_benchmark_full": bench_curve} if keep_full else {}
    return {**full,
        "strategy": strategy.describe(), "exit": exit_cfg.to_dict(), "risk": risk.to_dict(), "costs": costs.to_dict(),
        "period": {"start": em.get("start"), "end": em.get("end")},
        "metrics": {**em, **tm, "signals": net["signals"], "skipped_signals": net["skipped"], "halted_days": net["halted_days"]},
        "cost_impact": {"gross_total_return": gross_em["total_return"], "net_total_return": em["total_return"],
                        "difference": gross_em["total_return"] - em["total_return"],
                        "gross_trades": len(gross["trades"]),
                        "note": "비용 0 실행은 체결 수량·순서가 달라질 수 있어 거래 수가 다를 수 있음"},
        "benchmark": {"label": bench_label, **bench_m,
                      "excess_total_return": (em["total_return"] - bench_m["total_return"]) if bench_m else None},
        "random_baseline": random_dist,
        "by_regime": _regime_table(net["trades"], index_curve),
        "monthly": period_returns(eq, "M"), "yearly": period_returns(eq, "Y"),
        "equity_curve": _downsample(eq), "benchmark_curve": _downsample(bench_curve),
        "trades": net["trades"][-500:], "trades_truncated": len(net["trades"]) > 500,
        "data": data_info(frame, instruments), "assumptions": ASSUMPTIONS, "notes": notes,
    }


def compare_strategies(frame: pd.DataFrame, strategies: list[tuple[str, dict]], exit_cfg: ExitConfig, risk: RiskConfig,
                       costs: CostModel, benchmark: pd.DataFrame | None = None, instruments: pd.DataFrame | None = None,
                       extra: dict[str, Callable] | None = None) -> dict:
    """Same data, period, costs and fill rules for every strategy. `extra` adds filtered variants (e.g. ML)."""
    prepared = prepare(frame, benchmark)
    rows = []
    variants = [(k, p, None, k) for k, p in strategies]
    for label, (base_key, fn) in (extra or {}).items():
        variants.append((base_key, {}, fn, label))
    for key, params, filt, label in variants:
        strat = get_strategy(key, **params)
        if not strat.available:
            rows.append({"label": label, "strategy": key, "available": False, "reason": strat.unavailable_reason})
            continue
        r = run_portfolio(prepared, strat, exit_cfg, risk, costs, filt, instruments)
        em, tm = equity_metrics(r["equity"], risk.initial_cash), trade_metrics(r["trades"])
        rows.append({"label": label, "strategy": key, "name": strat.name, "available": True, "signals": r["signals"],
                     **{k: em.get(k) for k in ("total_return", "cagr", "max_drawdown", "sharpe", "volatility")},
                     **{k: tm.get(k) for k in ("trades", "win_rate", "win_rate_ci95", "expectancy_r", "expectancy_r_ci95",
                                              "profit_factor", "max_consecutive_losses", "sample_warning")}})
    dates = pd.concat([x["date"] for x in prepared.values()]) if prepared else pd.Series(dtype="datetime64[ns]")
    if benchmark is not None and not benchmark.empty:
        b = buy_and_hold_curve(benchmark, dates.drop_duplicates(), risk.initial_cash)
        label = "시장지수 매수 후 보유"
    else:
        b = equal_weight_curve(prepared, dates.drop_duplicates(), risk.initial_cash)
        label = "동일가중 매수 후 보유(대용)"
    bm = equity_metrics(b, risk.initial_cash) if not b.empty else {}
    rows.insert(0, {"label": label, "strategy": "buy_and_hold", "available": True, "trades": None,
                    **{k: bm.get(k) for k in ("total_return", "cagr", "max_drawdown", "sharpe", "volatility")}})
    return {"rows": rows, "data": data_info(frame, instruments), "exit": exit_cfg.to_dict(), "costs": costs.to_dict(),
            "note": "모든 전략에 같은 데이터·기간·비용·체결 규칙을 적용했다. 여러 전략을 비교할수록 우연히 좋아 보이는 결과가 나올 가능성이 커진다."}


def compare_exits(frame: pd.DataFrame, strategy_key: str, strategy_params: dict | None, variants: dict[str, dict],
                  costs: CostModel, benchmark: pd.DataFrame | None = None) -> dict:
    """Exit rules compared on IDENTICAL signals (each signal simulated independently, 1 unit)."""
    prepared = prepare(frame, benchmark)
    strat = get_strategy(strategy_key, **(strategy_params or {}))
    signals = {k: strat.signals(x) for k, x in prepared.items()}
    out = {}
    for label, cfg in variants.items():
        ec = ExitConfig.from_dict(cfg)
        out[label] = {"exit": ec.to_dict(), **summarize(signal_outcomes(prepared, signals, ec, costs))}
    return {"strategy": strat.describe(), "variants": out, "signals": int(sum(int(s.sum()) for s in signals.values())),
            "data": data_info(frame, None),
            "note": "같은 진입 신호를 청산 규칙만 바꿔 독립적으로 계산했다(포트폴리오 자금·보유 수 제약 없음). 데이터 끝까지 청산되지 않은 신호는 제외."}


def signal_study(frame: pd.DataFrame, strategy_key: str, strategy_params: dict | None, costs: CostModel,
                 benchmark: pd.DataFrame | None = None, symbol: tuple[str, str] | None = None) -> dict:
    prepared = prepare(frame, benchmark)
    if symbol:
        prepared = {k: v for k, v in prepared.items() if k == symbol}
    strat = get_strategy(strategy_key, **(strategy_params or {}))
    signals = {k: strat.signals(x) for k, x in prepared.items()}
    return {"strategy": strat.describe(), "forward": forward_returns(prepared, signals, costs=costs)}


def walk_forward(frame: pd.DataFrame, strategy_key: str, strategy_params: dict | None, base_exit: ExitConfig,
                 risk: RiskConfig, costs: CostModel, grid: dict[str, list] | None = None, n_folds: int = 4,
                 min_train_trades: int = 10, benchmark: pd.DataFrame | None = None,
                 instruments: pd.DataFrame | None = None) -> dict:
    """Pick exit parameters on each training window, apply them unchanged to the next test window."""
    grid = grid or {"target_r": [1.5, 2.0, 3.0], "atr_mult": [1.0, 1.5, 2.0]}
    combos = param_grid(grid)
    prepared = prepare(frame, benchmark)
    strat = get_strategy(strategy_key, **(strategy_params or {}))
    all_dates = pd.concat([x["date"] for x in prepared.values()]).drop_duplicates() if prepared else []
    folds = time_folds(all_dates, n_folds=n_folds, gap=base_exit.max_hold_days + 1)
    if not folds:
        raise ValueError("워크포워드에 필요한 기간이 부족합니다.")
    fold_rows, oos_trades, oos_returns = [], [], []
    for f in folds:
        best, best_score, best_is = None, -np.inf, None
        for c in combos:
            ec = ExitConfig.from_dict({**base_exit.to_dict(), **c})
            r = run_portfolio(prepared, strat, ec, risk, costs, None, instruments,
                              f.train_start.strftime("%Y-%m-%d"), f.train_end.strftime("%Y-%m-%d"))
            m = trade_metrics(r["trades"])
            if m["trades"] >= min_train_trades and m["expectancy_r"] is not None and m["expectancy_r"] > best_score:
                best, best_score, best_is = c, m["expectancy_r"], m
        row = {**f.to_dict(), "selected": best, "in_sample_expectancy_r": None if best is None else best_score,
               "in_sample_trades": None if best_is is None else best_is["trades"]}
        if best is None:
            row["note"] = f"학습 구간 거래가 {min_train_trades}건 미만인 설정뿐이라 선택하지 않음"
            fold_rows.append(row)
            continue
        ec = ExitConfig.from_dict({**base_exit.to_dict(), **best})
        r = run_portfolio(prepared, strat, ec, risk, costs, None, instruments,
                          f.test_start.strftime("%Y-%m-%d"), f.test_end.strftime("%Y-%m-%d"))
        m = trade_metrics(r["trades"])
        em = equity_metrics(r["equity"], risk.initial_cash)
        row.update({"oos_trades": m["trades"], "oos_expectancy_r": m["expectancy_r"], "oos_win_rate": m["win_rate"],
                    "oos_total_return": em["total_return"], "oos_max_drawdown": em["max_drawdown"]})
        oos_trades.extend(r["trades"])
        oos_returns.append(em["total_return"])
        fold_rows.append(row)
    agg = trade_metrics(oos_trades)
    chained = float(np.prod([1 + x for x in oos_returns]) - 1) if oos_returns else None
    return {"strategy": strat.describe(), "grid": grid, "configurations_tested_per_fold": len(combos), "folds": fold_rows,
            "out_of_sample": {**{k: agg[k] for k in ("trades", "win_rate", "win_rate_ci95", "expectancy_r", "expectancy_r_ci95",
                                                    "profit_factor", "sample_warning")}, "chained_total_return": chained},
            "data": data_info(frame, instruments),
            "note": ("각 학습 구간에서 기대값(R)이 가장 높은 청산 설정을 고르고, 그 설정을 바로 다음 구간에 그대로 적용했다. "
                     f"구간마다 {len(combos)}개 설정을 시험했으므로 학습 구간 성과는 과대평가되기 쉽다. 판단은 표본 외(OOS) 결과로 한다. "
                     f"학습·검증 사이 {base_exit.max_hold_days + 1}거래일 간격을 두었다.")}

