"""Entry-signal strategies. Each class is independent and is tested on its own.

A strategy only answers "is there an entry signal at the close of bar t?" using indicator rows
<= t. Entry is always the NEXT bar's open (handled by the backtest engine); exits, stops,
targets and position sizing come from `app.backtesting.ExitConfig` / `app.risk.RiskConfig`, so
the same exit rules can be compared fairly across strategies.
"""
from app.strategies.base import Strategy, STRATEGY_REGISTRY, get_strategy, list_strategies  # noqa: F401
from app.strategies import library  # noqa: F401  (registers the built-in strategies)
