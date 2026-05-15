-- btc_trades 신호 컨텍스트 컬럼 10개 추가
-- 2026-05-15: btc_trading_agent.py save_log()가 매 사이클 INSERT 시도하지만
-- 컬럼 누락으로 HTTP 400 → fallback chain에서 factor_snapshot 포함 모든
-- 부수 데이터가 minimal schema로 폴백 저장 중. PR #20c (factor_snapshot HOLD 확장)
-- 효과가 무력화된 진짜 원인.

ALTER TABLE btc_trades
    ADD COLUMN IF NOT EXISTS fg_value         NUMERIC,
    ADD COLUMN IF NOT EXISTS bb_pct           NUMERIC,
    ADD COLUMN IF NOT EXISTS vol_ratio_5m     NUMERIC,
    ADD COLUMN IF NOT EXISTS trend            TEXT,
    ADD COLUMN IF NOT EXISTS funding_rate     NUMERIC,
    ADD COLUMN IF NOT EXISTS oi_ratio         NUMERIC,
    ADD COLUMN IF NOT EXISTS ls_ratio         NUMERIC,
    ADD COLUMN IF NOT EXISTS kimchi           NUMERIC,
    ADD COLUMN IF NOT EXISTS market_regime    TEXT,
    ADD COLUMN IF NOT EXISTS composite_score  NUMERIC;

COMMENT ON COLUMN btc_trades.fg_value        IS 'Fear & Greed Index (0-100)';
COMMENT ON COLUMN btc_trades.bb_pct          IS 'Bollinger Band %B';
COMMENT ON COLUMN btc_trades.vol_ratio_5m    IS '5m volume ratio vs MA';
COMMENT ON COLUMN btc_trades.trend           IS 'Trend label (UP/DOWN/UNKNOWN)';
COMMENT ON COLUMN btc_trades.funding_rate    IS 'Perp funding rate';
COMMENT ON COLUMN btc_trades.oi_ratio        IS 'OI ratio';
COMMENT ON COLUMN btc_trades.ls_ratio        IS 'Long/Short ratio';
COMMENT ON COLUMN btc_trades.kimchi          IS '김치프리미엄 %';
COMMENT ON COLUMN btc_trades.market_regime   IS 'Regime: BULL/BEAR/SIDEWAYS/CRISIS';
COMMENT ON COLUMN btc_trades.composite_score IS '10-factor composite (0-100)';
