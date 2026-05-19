-- PR #25 BTC AI 결정 메타데이터 컬럼 (PR #29 Performance Layer 대비)
-- 5 컬럼 모두 NULLABLE → 기존 row 영향 0.
ALTER TABLE public.btc_trades
    ADD COLUMN IF NOT EXISTS decision_source TEXT,
    ADD COLUMN IF NOT EXISTS model           TEXT,
    ADD COLUMN IF NOT EXISTS ai_latency_ms   INTEGER,
    ADD COLUMN IF NOT EXISTS prompt_tokens   INTEGER,
    ADD COLUMN IF NOT EXISTS response_tokens INTEGER;
