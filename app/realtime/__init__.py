"""Real-time pieces that can be built and tested without a live feed.

Implemented: tick -> 1/5-minute OHLCV aggregation and feed-delay detection.
NOT implemented: the KIS WebSocket client (approval key, subscriptions, reconnect). It needs
KIS credentials and the current official WebSocket specification, which were not available
while building this version; see docs/PULSENTRA_STATUS.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def aggregate_ticks(ticks: pd.DataFrame, freq: str = "1min", tz: str = "Asia/Seoul") -> pd.DataFrame:
    """ticks: columns ts (timestamp, tz-aware or naive local), price, volume. Returns minute bars.

    Bars are labelled by their START time and closed on the left ([09:00, 09:01)). A bar exists
    only for minutes with at least one tick — empty minutes are not filled with fake prices.
    """
    if freq not in {"1min", "5min"}:
        raise ValueError("freq must be 1min or 5min")
    if ticks.empty:
        return pd.DataFrame(columns=["start", "open", "high", "low", "close", "volume", "turnover", "ticks"])
    t = ticks.copy()
    ts = pd.to_datetime(t["ts"])
    t["ts"] = ts.dt.tz_localize(tz) if ts.dt.tz is None else ts.dt.tz_convert(tz)
    t = t.sort_values("ts", kind="stable")
    if (t["price"] <= 0).any() or (t["volume"] < 0).any():
        raise ValueError("ticks must have price > 0 and volume >= 0")
    t["turnover"] = t["price"] * t["volume"]
    g = t.set_index("ts").groupby(pd.Grouper(freq=freq, label="left", closed="left"))
    bars = pd.DataFrame({"open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
                         "close": g["price"].last(), "volume": g["volume"].sum(), "turnover": g["turnover"].sum(),
                         "ticks": g["price"].count()})
    bars = bars[bars["ticks"] > 0].reset_index().rename(columns={"ts": "start"})
    return bars


def feed_delay_status(last_event: datetime | None, now: datetime | None = None, max_delay_seconds: float = 30.0,
                      market_open: bool = True) -> dict:
    """LIVE when the last tick is recent during market hours, DELAYED when late, ERROR when nothing arrived."""
    now = now or datetime.now(timezone.utc)
    if last_event is None:
        return {"status": "ERROR", "delay_seconds": None, "reason": "수신된 체결 데이터 없음"}
    if last_event.tzinfo is None:
        raise ValueError("last_event must be timezone-aware")
    delay = (now - last_event).total_seconds()
    if not market_open:
        return {"status": "DELAYED", "delay_seconds": delay, "reason": "장 운영시간 아님"}
    if delay > max_delay_seconds:
        return {"status": "DELAYED", "delay_seconds": delay, "reason": f"마지막 체결 수신 후 {delay:.0f}초 경과 (기준 {max_delay_seconds:.0f}초)"}
    return {"status": "LIVE", "delay_seconds": delay, "reason": "정상 수신"}
