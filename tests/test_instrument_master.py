from pathlib import Path

import pandas as pd
import pytest

from scripts.refresh_instrument_master import normalize_listing, write_snapshot


def test_normalize_listing_maps_market_and_keeps_instrument_metadata():
    source = pd.DataFrame([
        {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI", "Sector": "전기전자",
         "ListingDate": "1975-06-11", "Dept": "보통주"},
        {"Code": "035720", "Name": "카카오", "Market": "KOSDAQ GLOBAL", "Sector": "서비스",
         "ListingDate": "1999-11-11", "Dept": "보통주"},
    ])
    result = normalize_listing(source)
    assert result["symbol"].tolist() == ["005930", "035720"]
    assert result["market"].tolist() == ["KOSPI", "KOSDAQ"]
    assert result.loc[0, "name"] == "삼성전자"
    assert result.loc[0, "listed_date"] == "1975-06-11"


def test_normalize_listing_rejects_empty_or_duplicate_data():
    with pytest.raises(ValueError, match="empty"):
        normalize_listing(pd.DataFrame())
    duplicated = pd.DataFrame([
        {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
        {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"},
    ])
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_listing(duplicated)


def test_snapshot_writes_hash_and_provenance_without_price_data(tmp_path: Path):
    frame = normalize_listing(pd.DataFrame([
        {"Code": "005930", "Name": "삼성전자", "Market": "KOSPI"}
    ]))
    manifest = write_snapshot(frame, tmp_path, fetched_at="2026-10-30T00:00:00+00:00")
    assert manifest["rows"] == 1
    assert manifest["contains_market_prices"] is False
    assert manifest["redistribution_approved"] is False
    assert len(manifest["sha256"]) == 64
    assert (tmp_path / "krx_instruments.csv").exists()
    assert (tmp_path / "manifest.json").exists()
