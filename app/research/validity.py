"""What the research pipeline does and does not correct for, and the status a run's results deserve.

Each data treatment is classified from the code and tests as it stands — not from intent:
  handled      the code applies it and a test pins the behaviour
  partial      applied only in some cases, or applied with an unverified assumption
  not_handled  nothing in the pipeline accounts for it

`assess()` turns a run's facts into one of three result statuses:
  INELIGIBLE   a precondition for comparing strategies failed; numbers must not be used for any decision
  PROVISIONAL  numbers are internally consistent but rest on unverified data assumptions (the normal state today)
  VERIFIED     every listed verification has been done — never set automatically while any item is partial/not_handled
"""
from __future__ import annotations

HANDLED, PARTIAL, NOT_HANDLED = "handled", "partial", "not_handled"

DATA_TREATMENTS: list[dict] = [
    {"item": "split_merge_prices", "status": PARTIAL,
     "how": "Prices are taken as provider-adjusted (Naver; KIS FID_ORG_ADJ_PRC=0). Not cross-checked against an official source.",
     "evidence": "app/research/data.py (adjusted=True), docs/DATA_SOURCES.md"},
    {"item": "split_merge_volume", "status": PARTIAL,
     "how": "Volume is NOT adjusted by the provider. Level breaks across a trading gap are detected and the 40-bar windows that span them are excluded from signals.",
     "evidence": "app/data/quality.py volume_level_breaks, app/research/study.py prepare(); tests/test_research.py"},
    {"item": "bonus_issue_without_suspension", "status": NOT_HANDLED,
     "how": "Volume discontinuities without a trading gap are not detected (audit reports them only as unexplained level shifts when enabled).",
     "evidence": "app/data/quality.py volume_level_breaks docstring"},
    {"item": "corporate_action_disclosure_match", "status": NOT_HANDLED,
     "how": "Matching suspected events to OpenDART filings is implemented (app/research/crosscheck.py) but has never run on live data.",
     "evidence": "tests/test_crosscheck.py (mocked responses only)"},
    {"item": "cash_dividends", "status": NOT_HANDLED,
     "how": "Returns are price returns. Benchmarks are price indices. Long-horizon comparisons understate holding returns.",
     "evidence": "docs/DATA_SOURCES.md rule 5"},
    {"item": "transaction_costs_and_tax", "status": PARTIAL,
     "how": "Commission, sell tax and slippage are configurable assumptions applied per side; the sell tax is one rate for all years and markets.",
     "evidence": "app/risk CostModel, StudyConfig.costs, tests/test_backtest_engine.py::test_costs_reduce_pnl_exactly"},
    {"item": "delisted_instruments", "status": PARTIAL,
     "how": "Delisted common stocks from a third-party listing cache are included; completeness of that list is unverified. Positions are closed at the last close.",
     "evidence": "scripts/collect_kr_daily.py build_universe, app/backtesting/engine.py"},
    {"item": "listing_window", "status": PARTIAL,
     "how": "Listing dates are known only for delisted names in the universe file; bars outside a known window are flagged by the audit, not removed.",
     "evidence": "app/data/audit.py listing checks"},
    {"item": "trading_halts", "status": HANDLED,
     "how": "Provider halt-marker and flat zero-volume rows are removed before checks; positions wait for the next real bar and gaps fill at the open.",
     "evidence": "app/research/data.py load_naver_raw, tests/test_backtest_engine.py gap tests"},
    {"item": "liquidity_and_fill", "status": PARTIAL,
     "how": "Eligibility needs estimated 20-day turnover >= threshold and price >= threshold; no market-impact or tick-size model.",
     "evidence": "StudyConfig.min_avg_turnover/min_price"},
    {"item": "look_ahead_in_features", "status": HANDLED,
     "how": "Every indicator is tested for causality; signals use data up to t and fill at t+1 open.",
     "evidence": "tests/test_validation_and_ml.py::test_every_indicator_is_causal, test_signals_unchanged_by_future_shock"},
    {"item": "period_boundaries", "status": HANDLED,
     "how": "Signals within embargo_days of a period end are dropped so trades close inside their period; StudyConfig rejects an embargo shorter than the longest hold.",
     "evidence": "app/research/study.py period_of, StudyConfig.validate; tests/test_research_v04.py"},
    {"item": "point_in_time_universe", "status": PARTIAL,
     "how": "The universe is today's listing plus the delisting list; reused ticker codes keep only the current listing.",
     "evidence": "scripts/collect_kr_daily.py build_universe"},
    {"item": "cross_source_price_check", "status": NOT_HANDLED,
     "how": "Comparison against KIS daily bars is implemented (app/research/crosscheck.py) but has never run with a key.",
     "evidence": "tests/test_crosscheck.py (synthetic only)"},
]

# Facts a run may report that move the verification-dependent items. Keys are set by the caller only after a real check.
VERIFICATIONS = ("cross_source_price_check", "corporate_action_disclosure_match")


def coverage_table() -> list[dict]:
    return [dict(t) for t in DATA_TREATMENTS]


def assess(sample: dict, quality: dict | None = None, verified: dict[str, bool] | None = None,
           max_error_row_share: float = 0.01) -> dict:
    """Result status for a research run.

    sample:  app.research.data.sample_description(...)
    quality: {"rows_total": int, "rows_error": int} from the audit/loader (optional)
    verified: e.g. {"cross_source_price_check": True} only when that check actually ran and passed
    """
    verified = verified or {}
    inel, prov = [], []
    if not sample.get("bars"):
        inel.append("표본에 일봉이 없습니다.")
    if not sample.get("delisted_with_bars"):
        inel.append("상장폐지 종목 데이터가 없어 생존편향을 통제하지 못했습니다.")
    if quality and quality.get("rows_total"):
        share = quality.get("rows_error", 0) / quality["rows_total"]
        if share > max_error_row_share:
            inel.append(f"오류 행 비율 {share:.2%} > 허용 {max_error_row_share:.2%}")
    for t in DATA_TREATMENTS:
        if t["status"] == HANDLED:
            continue
        if t["item"] in VERIFICATIONS and verified.get(t["item"]):
            continue
        prov.append(f"{t['item']} ({t['status']})")
    status = "INELIGIBLE" if inel else ("PROVISIONAL" if prov else "VERIFIED")
    return {"status": status, "ineligible_reasons": inel, "unverified_items": prov}
