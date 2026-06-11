"""PR #33: US 주식/ETF 뉴스 수집(yfinance) + collect_news_once 라우팅 테스트."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.data import news_stream as ns


def test_fetch_us_stock_news_normalizes_yfinance_format():
    """yfinance 1.2.0 news 구조를 표준 row 포맷으로 정규화."""
    mock_news = [
        {
            "id": "test-uuid-1",
            "content": {
                "title": "SpaceX ETF surges on IPO buzz",
                "pubDate": "2026-06-11T10:00:00Z",
                "provider": {"displayName": "Reuters"},
                "canonicalUrl": {"url": "https://reuters.com/spacex"},
            },
        }
    ]
    mock_ticker = MagicMock()
    mock_ticker.news = mock_news
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = mock_ticker

    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        rows = ns._fetch_us_stock_news("ARKX", limit=20)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "test-uuid-1"
    assert row["headline"] == "SpaceX ETF surges on IPO buzz"
    assert row["source"] == "Reuters"
    assert row["symbols"] == ["ARKX"]
    assert row["sentiment_raw"] == 0.0
    assert row["url"] == "https://reuters.com/spacex"
    assert row["timestamp"].startswith("2026-06-11")


def test_fetch_us_stock_news_skips_empty_headline_and_uses_fallback_source():
    mock_news = [
        {"id": "no-title", "content": {"pubDate": "2026-06-11T10:00:00Z"}},
        {
            "id": "ok",
            "content": {
                "title": "Headline ok",
                "clickThroughUrl": {"url": "https://x.com/a"},
            },
        },
    ]
    mock_ticker = MagicMock()
    mock_ticker.news = mock_news
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = mock_ticker

    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        rows = ns._fetch_us_stock_news("XOVR", limit=20)

    # 빈 headline은 skip → 1건만, source fallback "Yahoo Finance"
    assert len(rows) == 1
    assert rows[0]["headline"] == "Headline ok"
    assert rows[0]["source"] == "Yahoo Finance"
    assert rows[0]["url"] == "https://x.com/a"


def test_fetch_us_stock_news_handles_exception_gracefully():
    mock_yf = MagicMock()
    mock_yf.Ticker.side_effect = RuntimeError("network down")
    with patch.dict("sys.modules", {"yfinance": mock_yf}):
        rows = ns._fetch_us_stock_news("SPCX", limit=20)
    assert rows == []


def test_collect_news_once_routes_us_ticker_to_yfinance(monkeypatch):
    called = []

    def fake_us(ticker, limit):
        called.append((ticker, limit))
        return [{"id": "1", "headline": "t", "source": "s",
                 "timestamp": "2026", "symbols": [ticker],
                 "sentiment_raw": 0.0, "url": ""}]

    monkeypatch.setattr(ns, "_fetch_us_stock_news", fake_us)
    monkeypatch.setattr(ns, "get_cached", lambda k: None)
    monkeypatch.setattr(ns, "set_cached", lambda k, v, ttl: None)

    rows = ns.collect_news_once(currencies="ARKX", limit=10)
    assert called == [("ARKX", 10)]
    assert len(rows) == 1


def test_collect_news_once_btc_stays_on_crypto_path(monkeypatch):
    crypto_called = []
    us_called = []

    monkeypatch.setattr(
        ns, "_fetch_cryptopanic_news",
        lambda **kw: crypto_called.append(kw) or [{"id": "c", "headline": "btc",
                                                   "source": "CP", "timestamp": "2026",
                                                   "symbols": ["BTC"], "sentiment_raw": 0.0,
                                                   "url": ""}],
    )
    monkeypatch.setattr(ns, "_fetch_us_stock_news",
                        lambda **kw: us_called.append(kw) or [])
    monkeypatch.setattr(ns, "get_cached", lambda k: None)
    monkeypatch.setattr(ns, "set_cached", lambda k, v, ttl: None)

    ns.collect_news_once(currencies="BTC", limit=10)
    assert crypto_called
    assert not us_called


def test_collect_news_once_explicit_asset_class_overrides(monkeypatch):
    us_called = []
    monkeypatch.setattr(ns, "_fetch_us_stock_news",
                        lambda ticker, limit: us_called.append(ticker) or [])
    monkeypatch.setattr(ns, "get_cached", lambda k: None)
    monkeypatch.setattr(ns, "set_cached", lambda k, v, ttl: None)

    # BTC지만 asset_class="stock" 강제 → US 경로
    ns.collect_news_once(currencies="BTC", limit=5, asset_class="stock")
    assert us_called == ["BTC"]
