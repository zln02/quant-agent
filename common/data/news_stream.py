"""Realtime-like news stream helpers for Phase 9.

This module currently uses polling with retry and cache to provide a stable,
callback-based stream interface.
"""
from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests

from common.cache import get_cached, set_cached
from common.env_loader import load_env
from common.logger import get_logger
from common.retry import retry_call

load_env()
log = get_logger("news_stream")

COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/developer/v2/posts/"


def _request_get(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: int = 8):
    return retry_call(
        requests.get,
        args=(url,),
        kwargs={"params": params, "headers": headers, "timeout": timeout},
        max_attempts=3,
        base_delay=1.0,
        default=None,
    )


def _extract_sentiment(votes) -> float:
    if not isinstance(votes, dict):
        return 0.0
    pos = float(votes.get("positive", 0) or 0)
    neg = float(votes.get("negative", 0) or 0)
    den = pos + neg
    if den <= 0:
        return 0.0
    return round((pos - neg) / den, 3)


def _parse_iso_timestamp(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    value = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _fetch_cryptopanic_news(currencies: str = "BTC", limit: int = 20) -> List[dict]:
    api_key = os.environ.get("CRYPTOPANIC_API_KEY", "")
    if not api_key:
        return []

    params = {
        "auth_token": api_key,
        "currencies": currencies,
        "public": "true",
        "kind": "news",
        "limit": limit,
    }
    res = _request_get(CRYPTOPANIC_URL, params=params)
    if res is None:
        log.warning("cryptopanic request failed after retries")
        return []
    if res.status_code == 429:
        # Rate limit: 30분 캐시로 요청 억제
        _rate_limit_cache = f"cryptopanic:rate_limit:{currencies}"
        set_cached(_rate_limit_cache, True, ttl=1800)
        log.warning("cryptopanic rate limited (429) — 30분 요청 억제", status=429)
        return []
    if not res.ok:
        log.warning("cryptopanic response not ok", status=res.status_code)
        return []

    try:
        rows = (res.json() or {}).get("results", [])
    except Exception as exc:
        log.error("cryptopanic json parse failed", error=exc)
        return []

    records: List[dict] = []
    for row in rows:
        source_obj = row.get("source") or {}
        symbols = [c.get("code") for c in row.get("currencies", []) if c.get("code")]
        ts = row.get("published_at") or row.get("created_at") or ""
        headline = row.get("title", "")
        if not headline:
            continue
        records.append(
            {
                "id": str(row.get("id") or row.get("slug") or f"{ts}:{headline}"),
                "headline": headline,
                "source": source_obj.get("title") or "CryptoPanic",
                "timestamp": _parse_iso_timestamp(ts),
                "symbols": symbols,
                "sentiment_raw": _extract_sentiment(row.get("votes")),
                "url": row.get("url", ""),
            }
        )
    return records


def _fetch_coindesk_rss(limit: int = 10) -> List[dict]:
    res = _request_get(COINDESK_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    if res is None or not res.ok:
        return []

    try:
        root = ET.fromstring(res.content)
    except Exception as exc:
        log.error("rss parse failed", error=exc)
        return []

    items = root.findall(".//item")[:limit]
    records: List[dict] = []
    for item in items:
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        pub = item.findtext("pubDate", "")
        if not title:
            continue
        records.append(
            {
                "id": link or f"rss:{title}",
                "headline": title,
                "source": "CoinDesk",
                "timestamp": pub,
                "symbols": ["BTC"],
                "sentiment_raw": 0.0,
                "url": link,
            }
        )
    return records


def _fetch_us_stock_news(ticker: str, limit: int = 20) -> List[dict]:
    """US 주식/ETF 종목별 뉴스를 yfinance 로 수집해 표준 row 포맷으로 정규화.

    CryptoPanic 은 암호화폐 전용이라 US 티커(SPCX/ARKX 등) 뉴스를 못 가져온다.
    yfinance 의 ``Ticker(ticker).news`` 는 무키로 종목별 뉴스를 제공한다.

    yfinance 1.2.0 news item 구조:
        {"id": "<uuid>", "content": {"title", "pubDate",
         "provider": {"displayName"}, "canonicalUrl": {"url"},
         "clickThroughUrl": {"url"}}}
    """
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — US news skipped", ticker=ticker)
        return []

    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as exc:
        log.warning("yfinance news fetch failed", ticker=ticker, error=exc)
        return []

    sym = ticker.strip().upper()
    records: List[dict] = []
    for item in raw[:limit]:
        content = item.get("content") or {}
        headline = content.get("title") or item.get("title") or ""
        if not headline:
            continue
        provider = content.get("provider") or {}
        url = (content.get("canonicalUrl") or {}).get("url") or (
            content.get("clickThroughUrl") or {}
        ).get("url") or item.get("link", "")
        ts = content.get("pubDate") or content.get("displayTime") or ""
        records.append(
            {
                "id": str(item.get("id") or url or f"{ts}:{headline}"),
                "headline": headline,
                "source": provider.get("displayName") or "Yahoo Finance",
                "timestamp": _parse_iso_timestamp(ts),
                "symbols": [sym],
                "sentiment_raw": 0.0,
                "url": url,
            }
        )
    return records


# 암호화폐 심볼 화이트리스트 — 여기 없는 심볼은 US 주식/ETF 로 간주해
# yfinance 경로를 탄다. 새 코인 추가 시 이 집합을 갱신할 것.
_CRYPTO_SYMBOLS = frozenset(
    {"BTC", "ETH", "XRP", "ADA", "SOL", "DOGE", "LTC", "MATIC", "DOT", "LINK", "UNI"}
)


def collect_news_once(
    currencies: str = "BTC", limit: int = 20, asset_class: str = "auto"
) -> List[dict]:
    """Collect normalized news records.

    Output schema:
    {
      headline, source, timestamp, symbols[], sentiment_raw, url, id
    }

    asset_class:
      "auto"   — 심볼을 화이트리스트로 판별 (crypto vs us stock). 기본값(하위호환).
      "crypto" — CryptoPanic/CoinDesk 경로 강제.
      "stock"  — yfinance US 종목 뉴스 경로 강제.
    """
    cache_key = f"news_stream:{currencies}:{limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    first_symbol = currencies.upper().split(",")[0].strip()
    if asset_class == "auto":
        is_crypto = first_symbol in _CRYPTO_SYMBOLS
    else:
        is_crypto = asset_class == "crypto"

    if not is_crypto:
        # US 주식/ETF — yfinance 종목 뉴스. crypto RSS fallback 안 탐.
        rows = _fetch_us_stock_news(ticker=first_symbol, limit=limit)
        set_cached(cache_key, rows, ttl=300)
        return rows

    # 429 rate limit 중이면 CryptoPanic 요청 건너뜀
    _rate_limit_key = f"cryptopanic:rate_limit:{currencies}"
    if get_cached(_rate_limit_key) is None:
        rows = _fetch_cryptopanic_news(currencies=currencies, limit=limit)
    else:
        rows = []
    if not rows:
        rows = _fetch_coindesk_rss(limit=min(limit, 10))

    set_cached(cache_key, rows, ttl=20)
    return rows


class NewsStream:
    """Polling-based callback stream for news events."""

    def __init__(self, currencies: str = "BTC", poll_interval: float = 20.0, limit: int = 20):
        self.currencies = currencies
        self.poll_interval = max(1.0, poll_interval)
        self.limit = limit
        self._callbacks: List[Callable[[dict], None]] = []
        self._seen_ids: set[str] = set()
        self._stop = threading.Event()

    def on_news(self, callback: Callable[[dict], None]) -> None:
        self._callbacks.append(callback)

    def stop(self) -> None:
        self._stop.set()

    def pump_once(self) -> List[dict]:
        rows = collect_news_once(currencies=self.currencies, limit=self.limit)
        if not rows:
            return []

        fresh: List[dict] = []
        for item in reversed(rows):  # deliver oldest->newest
            item_id = str(item.get("id", ""))
            if not item_id or item_id in self._seen_ids:
                continue
            self._seen_ids.add(item_id)
            fresh.append(item)
            for cb in self._callbacks:
                try:
                    cb(item)
                except Exception as exc:
                    log.error("news callback failed", error=exc)
        return fresh

    def run_forever(self) -> None:
        log.info("news stream started", currencies=self.currencies, poll_interval=self.poll_interval)
        while not self._stop.is_set():
            try:
                self.pump_once()
            except Exception as exc:
                log.error("news stream loop failed", error=exc)
            self._stop.wait(self.poll_interval)


if __name__ == "__main__":
    stream = NewsStream(currencies="BTC", poll_interval=15)

    def _printer(item: Dict):
        log.info("news", headline=item.get("headline"), source=item.get("source"))

    stream.on_news(_printer)
    try:
        stream.run_forever()
    except KeyboardInterrupt:
        stream.stop()
        log.info("news stream stopped")
