-- Phase 5-E Step 7: 테이블·컬럼 주석 (COMMENT ON)
-- 목적: 스키마 문서화. Supabase Studio와 DBeaver에서 즉시 확인 가능.

-- === 시장 데이터 ===
COMMENT ON TABLE public.daily_ohlcv IS 'KR 일별 OHLCV. PK=(stock_code,date). stock_code는 KRX 6자리.';
COMMENT ON TABLE public.intraday_ohlcv IS 'KR 분/시간 봉. UNIQUE=(stock_code,datetime,time_interval). time_interval 예: 5m/1h/1d.';
COMMENT ON TABLE public.stock_ohlcv IS 'DEPRECATED 후보 — daily_ohlcv와 중복 (20 rows만 존재). 삭제는 별도 Phase.';
COMMENT ON TABLE public.btc_candles IS 'BTC OHLCV (Upbit). UNIQUE=(timestamp,interval). minute5/minute60/day 등.';
COMMENT ON TABLE public.btc_alt_data IS 'BTC 대체 지표 (김치프리미엄·Fear&Greed·USD/KRW 환율).';
COMMENT ON TABLE public.top50_stocks IS 'KR 유니버스 시총 상위 50선. 주기적 갱신.';
COMMENT ON TABLE public.financial_statements IS '재무제표 (PER/PBR/ROE·매출). stock_code 기준 최신값 (fiscal_year로 별도 UNIQUE).';
COMMENT ON TABLE public.us_momentum_signals IS 'US 모멘텀 스코어 (run_date 기준). score, ret_5d, ret_20d, vol_ratio.';
COMMENT ON TABLE public.disclosures IS '공시 데이터 (저활용 3 rows). 향후 재설계 대상.';

-- === 체결 ===
COMMENT ON TABLE public.trade_executions IS
    'KR 체결 기록. trade_type=BUY|SELL, result=OPEN|CLOSED|STOPPED|CLOSED_SYNC|SYNC_ERROR. signal_source=rule|ml|llm|composite|manual.';

COMMENT ON TABLE public.us_trade_executions IS
    'US 체결 기록 (yfinance 드라이런 포함). signal_source 사용 권장. source 컬럼은 legacy.';

COMMENT ON TABLE public.btc_trades IS
    'BTC 신호/체결 로그. action=BUY|SELL|HOLD (HOLD는 판단만 기록), signal_source는 판단 로직 소스.';

COMMENT ON TABLE public.btc_position IS
    'BTC 열린 포지션 + 청산 기록. status=OPEN|CLOSED. 다수 파생 필드(fg_value,rsi_d,bb_pct,vol_ratio_d,…)는 진입 시점 스냅샷.';

COMMENT ON TABLE public.trade_snapshots IS
    'DEPRECATED 후보 — rows=0. trade_executions에 factor_snapshot(jsonb)로 대체됨.';

COMMENT ON TABLE public.execution_quality IS
    'SmartRouter 실행 품질 기록 (expected/actual price, slippage_bps). 현재 rows=0 — 코드 통합 미완, Phase 3(실행·라우팅)에서 활성화 예정.';

-- === 리스크·운영 상태 ===
COMMENT ON TABLE public.drawdown_guard_state IS
    '드로다운 가드 현재 상태. PK=market (btc|kr|us). cooldown_until 이후 거래 재개.';

COMMENT ON TABLE public.circuit_breaker_events IS
    '서킷브레이커 트리거 이벤트 로그. trigger_level=WARN|CRITICAL|FULL, action_taken=reduce|halt 등.';

COMMENT ON TABLE public.health_snapshots IS
    '5분 주기 헬스체크 스냅샷 (dashboard/docker/supabase/memory/disk). ~52 MB. retention/파티셔닝은 별도 Phase.';

-- === 분석·리포트 ===
COMMENT ON TABLE public.signal_ic_history IS
    '신호별 IC/IR 주간 측정 히스토리. UNIQUE=(date,signal). active 플래그로 사용 on/off.';

COMMENT ON TABLE public.daily_reports IS
    '일일 프리마켓/애프터마켓 리포트. report_type=premarket|aftermarket, content(jsonb)에 구조화 지표.';

COMMENT ON TABLE public.data_collection_log IS
    'DEPRECATED 후보 — rows=0. health_snapshots로 대체됨.';
