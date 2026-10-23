# Strategy Spec — Volume Expansion / Quiet Price

## Research hypothesis

An unusually large increase in trading activity while price remains range-bound may identify instruments worth monitoring. It does **not** establish accumulation or predict a rise by itself. The strategy should enter only after a separately testable confirmation rule, and compare against simpler baselines.

## V1 screen definition

For a bar at date `t`:

- `volume_ratio_20 = sum(volume[t-19:t]) / sum(volume[t-39:t-20])`
- `turnover_ratio_20 = sum(turnover[t-19:t]) / sum(turnover[t-39:t-20])`
- `ret_20 = close[t] / close[t-20] - 1`
- `ATR14` = rolling mean of True Range over 14 bars (MVP approximation, Wilder smoothing not yet used)
- `CLV = (2*close - high - low)/(high-low)`; `CLV20` is volume-weighted mean over 20 bars

Core screen defaults:

- `volume_ratio_20 >= 3.0`
- `-0.03 <= ret_20 <= +0.03`
- `turnover_ratio_20 >= 1.5`

Optional confirmation filters:

- positive `CLV20`
- close above 60-session moving average
- positive 60-session return / market-relative strength
- no unresolved material corporate-action or financing risk

## V1 strategy/backtest definition

- Signal at close of date `t` (features use only data available at that close).
- Enter long at the open of next trading session `t+1`.
- Stop: `entry - ATR_stop_multiple * ATR14(t)`; default multiple 1.5.
- Target: `entry + reward_risk * (entry - stop)`; default 2R.
- Time exit after 10 sessions by default.
- If daily high/low imply stop and target are both hit in one candle, count stop first (conservative because daily candles do not reveal intraday sequence).
- If opening gap passes the stop, fill at open in the MVP. Other real-world order behavior is not modelled.
- Round trip costs are an editable basis-points assumption; default is illustrative and not a broker quote.

## Baselines to compare

1. Buy-and-hold / market-index benchmark.
2. Same instrument sampled on random eligible days.
3. Volume expansion only (no price-consolidation filter).
4. Volume expansion + price consolidation.
5. Core screen + positive CLV.
6. Core screen + breakout confirmation.
7. Core screen + relative-strength filter.
8. ATR stop/target variants: stop 1.0, 1.5, 2.0 ATR; targets 1.5R, 2R, 3R; holding 5, 10, 20 sessions.

## Evaluation

Report at least: signal/trade count, win rate, mean and median net return, expectancy per trade, Profit Factor, max drawdown, exposure, average holding period, benchmark-relative return, and bootstrap confidence interval. Split chronologically; reserve a final untouched period. Use expanding/rolling walk-forward tests. Treat repeated tuning as multiple-hypothesis testing; document the number of configurations examined.

## Known MVP limitations

- No survivorship-bias-free point-in-time universe yet.
- Corporate actions/dividends are not handled automatically; data must be consistently adjusted.
- No full portfolio simulator (cash, simultaneous positions, correlation, and position sizing).
- Price fills and intraday stop/target sequence are approximations.
- The current ML label is a simple triple-barrier-style classification, not a complete portfolio policy.
- CLV20 is a price-location proxy, not proof of institutional accumulation.

## Pulsentra v0.2 implementation notes

- Implemented as `app/strategies/library.py`: `volume_quiet` (immediate entry), `volume_quiet_breakout` (setup within 10 sessions + close above the prior 20-session high), `volume_surge_only` (baseline without the price filter), `breakout_volume`, `pullback_uptrend`, `ma_trend` (simple baseline), `rsi_rebound`, `bb_squeeze`, `momentum_rs` (needs a benchmark), `random_entry` (baseline). `disclosure_event` is registered as NOT available.
- ATR(14) keeps the MVP simple-mean definition for stops; `atr_14_wilder` is computed alongside.
- `clv_20` is, by definition, Chaikin Money Flow(20); it is exposed as `cmf_20` as well.
- Exit variants on identical signals: `POST /api/backtests/exits`. Walk-forward exit-parameter selection: `POST /api/backtests/walk-forward` (reports the number of configurations tested per fold).
- Every formula shown in the UI comes from `app/indicators.py::INDICATOR_SPECS`.
