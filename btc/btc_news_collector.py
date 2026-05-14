# btc_news_collector.py
import os

import requests


def get_news_summary() -> str:
    """CryptoPanic v2 API로 BTC 실시간 뉴스 수집"""
    api_key = os.environ.get("CRYPTOPANIC_API_KEY", "")

    if not api_key:
        return "뉴스 API 키 없음 — 지표만으로 판단"

    try:
        res = requests.get(
            "https://cryptopanic.com/api/developer/v2/posts/",
            params={
                "auth_token": api_key,
                "currencies": "BTC",
                "public": "true",
            },
            timeout=5,
        )
        if res.status_code != 200:
            return f"뉴스 API 오류: HTTP {res.status_code}"
        data = res.json()
        posts = data.get("results", [])[:5]

        if not posts:
            return "최근 BTC 뉴스 없음"

        # 긍정/부정 키워드로 간단 감정 분석
        POS_KEYWORDS = [
            "surge", "rally", "bullish", "gain", "rise", "high",
            "adoption", "approval", "buy", "support", "breakthrough",
            "상승", "급등", "호재", "매수", "승인", "돌파",
        ]
        NEG_KEYWORDS = [
            "drop", "fall", "bearish", "crash", "fear", "ban",
            "sell", "decline", "warning", "risk", "hack", "fraud",
            "하락", "급락", "악재", "매도", "규제", "해킹", "사기",
        ]

        positive, negative = 0, 0
        headlines = []

        for p in posts:
            title = p.get("title", "")
            desc = p.get("description", "")
            text = (title + " " + desc).lower()

            pos = sum(1 for k in POS_KEYWORDS if k in text)
            neg = sum(1 for k in NEG_KEYWORDS if k in text)
            positive += pos
            negative += neg

            if pos > neg:
                emoji = "🟢"
            elif neg > pos:
                emoji = "🔴"
            else:
                emoji = "⚪"

            headlines.append(f"{emoji} {title}")

        # 전체 감정
        if positive > negative + 2:
            sentiment = f"🟢 긍정적 (긍정{positive} vs 부정{negative})"
        elif negative > positive + 2:
            sentiment = f"🔴 부정적 (긍정{positive} vs 부정{negative})"
        else:
            sentiment = f"⚪ 중립 (긍정{positive} vs 부정{negative})"

        return f"[뉴스 감정: {sentiment}]\n" + "\n".join(headlines)

    except Exception as e:
        return f"뉴스 수집 실패: {e}"


def get_news_result() -> dict:
    """CryptoPanic 뉴스 수집 + 수치화된 감정 점수 반환.

    Returns:
        {"summary": str, "score": float (-1.0~+1.0), "positive": int, "negative": int}
    """
    api_key = os.environ.get("CRYPTOPANIC_API_KEY", "")

    if not api_key:
        return {"summary": "뉴스 API 키 없음 — 지표만으로 판단", "score": 0.0, "positive": 0, "negative": 0}

    try:
        res = requests.get(
            "https://cryptopanic.com/api/developer/v2/posts/",
            params={"auth_token": api_key, "currencies": "BTC", "public": "true"},
            timeout=5,
        )
        if res.status_code != 200:
            return {"summary": f"뉴스 API 오류: HTTP {res.status_code}", "score": 0.0, "positive": 0, "negative": 0}
        data = res.json()
        posts = data.get("results", [])[:5]

        if not posts:
            return {"summary": "최근 BTC 뉴스 없음", "score": 0.0, "positive": 0, "negative": 0}

        POS_KEYWORDS = [
            "surge", "rally", "bullish", "gain", "rise", "high",
            "adoption", "approval", "buy", "support", "breakthrough",
            "상승", "급등", "호재", "매수", "승인", "돌파",
        ]
        NEG_KEYWORDS = [
            "drop", "fall", "bearish", "crash", "fear", "ban",
            "sell", "decline", "warning", "risk", "hack", "fraud",
            "하락", "급락", "악재", "매도", "규제", "해킹", "사기",
        ]

        positive, negative = 0, 0
        headlines = []

        for p in posts:
            title = p.get("title", "")
            desc = p.get("description", "")
            text = (title + " " + desc).lower()
            pos_cnt = sum(1 for k in POS_KEYWORDS if k in text)
            neg_cnt = sum(1 for k in NEG_KEYWORDS if k in text)
            positive += pos_cnt
            negative += neg_cnt
            emoji = "🟢" if pos_cnt > neg_cnt else ("🔴" if neg_cnt > pos_cnt else "⚪")
            headlines.append(f"{emoji} {title}")

        # 감정 점수 정규화 (-1.0 ~ +1.0)
        total_signals = positive + negative
        score = round((positive - negative) / max(total_signals, 1), 2) if total_signals > 0 else 0.0
        score = max(-1.0, min(1.0, score))

        if positive > negative + 2:
            sentiment = f"🟢 긍정적 (긍정{positive} vs 부정{negative})"
        elif negative > positive + 2:
            sentiment = f"🔴 부정적 (긍정{positive} vs 부정{negative})"
        else:
            sentiment = f"⚪ 중립 (긍정{positive} vs 부정{negative})"

        summary = f"[뉴스 감정: {sentiment}]\n" + "\n".join(headlines)
        return {"summary": summary, "score": score, "positive": positive, "negative": negative}

    except Exception as e:
        return {"summary": f"뉴스 수집 실패: {e}", "score": 0.0, "positive": 0, "negative": 0}


# btc_trading_agent.py 호환용
collect_news_summary = get_news_summary


def persist_to_db(posts: list, sentiment_score: float, source: str = "cryptopanic") -> int:
    """news_articles 테이블에 upsert. 거래 사이클에서는 호출 금지(raise 흡수)."""
    try:
        from common.logger import get_logger
        from common.supabase_client import get_supabase
        sb = get_supabase()
        log = get_logger("btc_news_persist")
        if not sb or not posts:
            return 0

        label = ("positive" if sentiment_score > 0.2
                 else "negative" if sentiment_score < -0.2
                 else "neutral")
        rows = []
        for p in posts:
            url = p.get("url")
            if not url:
                continue
            rows.append({
                "market": "btc",
                "symbol": "BTC",
                "source": source,
                "headline": p.get("title", ""),
                "content": p.get("description"),
                "url": url,
                "sentiment_score": sentiment_score,
                "sentiment_label": label,
                "published_at": p.get("published_at"),
            })
        if not rows:
            return 0

        sb.table("news_articles").upsert(rows, on_conflict="url").execute()
        log.info(f"뉴스 {len(rows)}건 영속화")
        return len(rows)
    except Exception as e:
        try:
            from common.logger import get_logger
            get_logger("btc_news_persist").warning(f"뉴스 영속화 실패: {e}")
        except Exception:
            pass
        return 0
