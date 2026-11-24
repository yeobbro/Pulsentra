import os
import sys
import tempfile
from pathlib import Path

# An isolated data directory for the whole test session — never the user's real database.
_TMP = tempfile.mkdtemp(prefix="pulsentra-test-")
os.environ["PULSENTRA_DATA_DIR"] = _TMP
os.environ["PULSENTRA_DB_PATH"] = str(Path(_TMP) / "test.sqlite3")
for key in ("KIS_APP_KEY", "KIS_APP_SECRET", "DART_API_KEY", "KRX_API_KEY"):   # never the real keys from .env
    os.environ[key] = ""
os.environ["KIS_MIN_INTERVAL_SECONDS"] = "0"
# Hosting settings from a developer .env must not change the test app: no access guard, no DEMO autoload.
for key in ("PULSENTRA_ACCESS_USERNAME", "PULSENTRA_ACCESS_PASSWORD"):
    os.environ[key] = ""
os.environ["PULSENTRA_ENV"] = "development"
os.environ["PULSENTRA_AUTOLOAD_DEMO"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402


def make_bars(close, symbol="T001", market="KR", start="2024-01-01", volume=None, spread=1.0, opens=None, highs=None, lows=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    dates = pd.bdate_range(start, periods=n)
    volume = np.full(n, 1000.0) if volume is None else np.asarray(volume, dtype=float)
    o = close.copy() if opens is None else np.asarray(opens, dtype=float)
    h = np.maximum(o, close) + spread if highs is None else np.asarray(highs, dtype=float)
    l = np.minimum(o, close) - spread if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({"market": market, "symbol": symbol, "date": dates, "open": o, "high": h, "low": l,
                         "close": close, "volume": volume, "turnover": close * volume, "source": "test"})


@pytest.fixture
def bars():
    return make_bars


@pytest.fixture
def tmp_db(tmp_path):
    from app import db
    path = tmp_path / "t.sqlite3"
    db.init_db(path)
    return path
