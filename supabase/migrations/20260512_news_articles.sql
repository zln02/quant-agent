-- BTC/KR/US 뉴스 영속화 테이블
-- PR #20 (2026-05-12): cryptopanic 호출은 매 사이클 발생하지만 DB 저장이 없어 학습/회고 불가.
-- 향후 KR(naver)/US(GDELT/SEC) 소스 확장은 PR #22.

CREATE TABLE IF NOT EXISTS news_articles (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('btc','kr','us')),
    symbol TEXT,
    source TEXT NOT NULL,                  -- cryptopanic | naver | gdelt | sec
    headline TEXT NOT NULL,
    content TEXT,
    url TEXT NOT NULL UNIQUE,
    sentiment_score NUMERIC,               -- -1.0 ~ +1.0
    sentiment_label TEXT,                  -- positive | negative | neutral
    impact_score NUMERIC,                  -- PR #21 (FinBERT) 예약
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_articles_market_published
    ON news_articles (market, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_news_articles_symbol_published
    ON news_articles (symbol, published_at DESC)
    WHERE symbol IS NOT NULL;

COMMENT ON TABLE news_articles IS
    'BTC/KR/US 뉴스 영속화. PR #20 (2026-05-12). 중복 제거: url UNIQUE.';

-- RLS: Phase 5-E 패턴 동일 — service_role ALL 정책
ALTER TABLE news_articles ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'news_articles'
          AND policyname = 'service_role_all_news_articles'
    ) THEN
        CREATE POLICY service_role_all_news_articles
            ON public.news_articles
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;
