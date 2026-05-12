#!/usr/bin/env python3
"""BTC 뉴스 영속화 cron. cryptopanic v2 → news_articles."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.env_loader import load_env

load_env()

from btc.btc_news_collector import get_news_result, persist_to_db
from common.logger import get_logger

log = get_logger("collect_btc_news")


def _fetch_raw_posts() -> list:
    """get_news_result()는 처리 dict만 반환 — raw posts 별도 fetch (최대 20개)."""
    api_key = os.environ.get("CRYPTOPANIC_API_KEY", "")
    if not api_key:
        log.warning("CRYPTOPANIC_API_KEY 미설정")
        return []
    try:
        res = requests.get(
            "https://cryptopanic.com/api/developer/v2/posts/",
            params={"auth_token": api_key, "currencies": "BTC", "public": "true"},
            timeout=10,
        )
        if res.status_code != 200:
            log.warning(f"cryptopanic HTTP {res.status_code}")
            return []
        return res.json().get("results", [])[:20]
    except Exception as e:
        log.warning(f"cryptopanic fetch 실패: {e}")
        return []


if __name__ == "__main__":
    posts = _fetch_raw_posts()
    if not posts:
        log.info("뉴스 없음")
        sys.exit(0)
    result = get_news_result()
    saved = persist_to_db(posts, result.get("score", 0.0))
    log.info(f"뉴스 cron 완료: fetched={len(posts)} saved={saved}")
