-- review_logs: Daily Reviewer (PR #28) LLM 리뷰 결과 저장
-- 매일 장 마감 후 cron으로 daily_reviewer.py가 INSERT.
-- append-only (재실행 시 새 row 추가, UNIQUE 미적용).

CREATE TABLE IF NOT EXISTS public.review_logs (
    id          BIGSERIAL    PRIMARY KEY,
    market      TEXT         NOT NULL,                                       -- 'btc' | 'kr' | 'us'
    window_end  TIMESTAMPTZ  NOT NULL,                                       -- 리뷰 대상 24h 윈도우 종료 시각
    raw_data    JSONB        NOT NULL DEFAULT '{}'::jsonb,                   -- 수집된 4섹션 ctx 원본
    review_text TEXT,                                                        -- Haiku 생성 한국어 리뷰 본문
    model       TEXT         NOT NULL DEFAULT 'claude-haiku-4-5-20251001',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 마켓별 최신 리뷰 조회용
CREATE INDEX IF NOT EXISTS idx_review_logs_market_window
    ON public.review_logs (market, window_end DESC);
