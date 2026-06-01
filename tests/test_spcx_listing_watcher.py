"""PR #29: SPCX 상장 감지 watcher 테스트."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd


def test_has_listed_data_returns_true_when_yfinance_has_bars(monkeypatch):
    from scripts import spcx_listing_watcher as w

    mock_hist = pd.DataFrame({
        "Close": [120.5, 122.3, 119.8],
    }, index=pd.to_datetime(["2026-06-12", "2026-06-13", "2026-06-14"]))

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_hist
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = mock_ticker

    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        listed, meta = w._has_listed_data()

    assert listed is True
    assert meta["bars"] == 3
    assert meta["last_close"] == 119.8
    assert meta["last_date"] == "2026-06-14"


def test_has_listed_data_returns_false_for_empty_history(monkeypatch):
    from scripts import spcx_listing_watcher as w

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = mock_ticker

    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        listed, meta = w._has_listed_data()

    assert listed is False
    assert meta["bars"] == 0


def test_has_listed_data_handles_exception(monkeypatch):
    from scripts import spcx_listing_watcher as w

    mock_yf = MagicMock()
    mock_yf.Ticker.side_effect = RuntimeError("network down")

    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        listed, meta = w._has_listed_data()

    assert listed is False
    assert meta["source"] == "yfinance_error"


def test_check_and_notify_sends_alert_first_time(tmp_path, monkeypatch):
    from scripts import spcx_listing_watcher as w

    monkeypatch.setattr(w, "_SENTINEL_FILE", tmp_path / "spcx_listed.flag")
    monkeypatch.setattr(w, "_has_listed_data",
                        lambda: (True, {"last_close": 200.0, "last_date": "2026-06-12", "bars": 1}))

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(w, "send_telegram", sent)

    result = w.check_and_notify()

    assert result["listed"] is True
    assert result["alert_sent"] is True
    assert sent.called
    msg = sent.call_args[0][0]
    assert "SPCX 상장 감지" in msg
    assert "$200.0" in msg
    # sentinel 생성됨
    assert (tmp_path / "spcx_listed.flag").exists()


def test_check_and_notify_skips_when_sentinel_exists(tmp_path, monkeypatch):
    from scripts import spcx_listing_watcher as w

    sentinel = tmp_path / "spcx_listed.flag"
    sentinel.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(w, "_SENTINEL_FILE", sentinel)
    monkeypatch.setattr(w, "_has_listed_data",
                        lambda: (True, {"last_close": 200.0}))

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(w, "send_telegram", sent)

    result = w.check_and_notify()

    assert result["listed"] is True
    assert result["alert_sent"] is False
    assert not sent.called


def test_check_and_notify_force_overrides_sentinel(tmp_path, monkeypatch):
    from scripts import spcx_listing_watcher as w

    sentinel = tmp_path / "spcx_listed.flag"
    sentinel.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(w, "_SENTINEL_FILE", sentinel)
    monkeypatch.setattr(w, "_has_listed_data",
                        lambda: (True, {"last_close": 200.0}))

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(w, "send_telegram", sent)

    result = w.check_and_notify(force=True)

    assert result["alert_sent"] is True
    assert sent.called


def test_check_and_notify_not_listed_no_alert(tmp_path, monkeypatch):
    from scripts import spcx_listing_watcher as w

    monkeypatch.setattr(w, "_SENTINEL_FILE", tmp_path / "spcx_listed.flag")
    monkeypatch.setattr(w, "_has_listed_data",
                        lambda: (False, {"source": "no_data", "bars": 0}))

    sent = MagicMock(return_value=True)
    monkeypatch.setattr(w, "send_telegram", sent)

    result = w.check_and_notify()

    assert result["listed"] is False
    assert result["alert_sent"] is False
    assert not sent.called
    assert not (tmp_path / "spcx_listed.flag").exists()


def test_sentinel_writes_metadata_json(tmp_path, monkeypatch):
    from scripts import spcx_listing_watcher as w

    monkeypatch.setattr(w, "_SENTINEL_FILE", tmp_path / "spcx_listed.flag")
    monkeypatch.setattr(w, "_has_listed_data",
                        lambda: (True, {"last_close": 200.5, "last_date": "2026-06-12", "bars": 1}))
    monkeypatch.setattr(w, "send_telegram", MagicMock(return_value=True))

    w.check_and_notify()

    saved = json.loads((tmp_path / "spcx_listed.flag").read_text(encoding="utf-8"))
    assert saved["last_close"] == 200.5
    assert saved["last_date"] == "2026-06-12"
