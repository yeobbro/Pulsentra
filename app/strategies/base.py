from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

STRATEGY_REGISTRY: dict[str, type["Strategy"]] = {}


class Strategy(ABC):
    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    hypothesis: ClassVar[str] = ""
    defaults: ClassVar[dict[str, Any]] = {}
    requires: ClassVar[tuple[str, ...]] = ()        # e.g. ("benchmark",) or ("disclosures",)
    available: ClassVar[bool] = True
    unavailable_reason: ClassVar[str] = ""

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.key}: unknown parameter(s) {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.key:
            STRATEGY_REGISTRY[cls.key] = cls

    @abstractmethod
    def _signal(self, x: pd.DataFrame) -> pd.Series:
        """Boolean-like Series aligned with x (indicator frame of one instrument)."""

    def signals(self, x: pd.DataFrame) -> pd.Series:
        if x.empty:
            return pd.Series(dtype=bool)
        s = self._signal(x)
        # Unknown (NaN) is never a signal.
        return s.astype("float").fillna(0.0).astype(bool).reindex(x.index, fill_value=False)

    def _exit_signal(self, x: pd.DataFrame) -> pd.Series | None:
        """Optional exit rule evaluated at the close of bar t (rows <= t only). None = no own exit rule."""
        return None

    def exit_signals(self, x: pd.DataFrame) -> pd.Series | None:
        if x.empty:
            return None
        s = self._exit_signal(x)
        if s is None:
            return None
        return s.astype("float").fillna(0.0).astype(bool).reindex(x.index, fill_value=False)

    @property
    def has_exit_rule(self) -> bool:
        return type(self)._exit_signal is not Strategy._exit_signal

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "hypothesis": self.hypothesis, "params": self.params,
                "requires": list(self.requires), "has_exit_rule": self.has_exit_rule, "available": self.available, "unavailable_reason": self.unavailable_reason}


def col(x: pd.DataFrame, name: str) -> pd.Series:
    return x[name] if name in x else pd.Series(np.nan, index=x.index)


def get_strategy(key: str, **params: Any) -> Strategy:
    if key not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown strategy '{key}'. available: {sorted(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[key](**params)


def list_strategies() -> list[dict]:
    return [cls().describe() for cls in STRATEGY_REGISTRY.values()]
