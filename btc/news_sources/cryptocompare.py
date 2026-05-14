"""CryptoCompare News API — BTC 뉴스 수집기.

응답을 cryptopanic 형식으로 정규화하여 기존 persist_to_db에 그대로 투입.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from common.logger import get_logger

log = get_logger("btc.cryptocompare")
BASE_URL = "https://min-api.cryptocompare.com/data/v2/news/"


def fetch_btc_news(limit: int = 50) -> list[dict]:
    """CryptoCompare → normalized posts (cryptopanic 호환 dict).

    Returns:
        [{"title", "description", "url", "published_at"}, ...]
    """
    api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
    if not api_key:
        log.warning("CRYPTOCOMPARE_API_KEY 미설정")
        return []
    try:
        res = requests.get(
            BASE_URL,
            params={
                "api_key": api_key,
                "categories": "BTC",
                "excludeCategories": "Sponsored",
                "lang": "EN",
            },
            timeout=10,
        )
        if res.status_code != 200:
            log.warning(f"cryptocompare HTTP {res.status_code}")
            return []
        raw = res.json().get("Data", [])[:limit]
        normalized: list[dict] = []
        for p in raw:
            url = p.get("url")
            if not url:
                continue
            ts = p.get("published_on")
            published_at = (
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                if isinstance(ts, int) else None
            )
            normalized.append({
                "title": p.get("title", ""),
                "description": p.get("body", "") or "",
                "url": url,
                "published_at": published_at,
            })
        return normalized
    except Exception as e:
        log.warning(f"cryptocompare fetch 실패: {e}")
        return []


if __name__ == "__main__":
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from common.env_loader import load_env
    load_env()
    from btc.btc_news_collector import persist_to_db

    posts = fetch_btc_news(limit=20)
    log.info(f"Fetched {len(posts)} articles from CryptoCompare")
    if posts:
        saved = persist_to_db(posts, sentiment_score=0.0, source="cryptocompare")
        log.info(f"Persisted {saved} rows (source=cryptocompare)")
