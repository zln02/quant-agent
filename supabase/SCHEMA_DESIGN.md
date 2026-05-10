# DB 스키마 설계 문서 (Phase 5-E)

Supabase `my-openclaw` (`tgbwciiwxggvvnwbhrkx`) public schema 정규화 대상.
Postgres 17. 2026-04-19 기준. quant-agent 관련 23개 테이블 범위 (`jay_users` 제외).

---

## 1. 개념 모델 (Entity / Domain)

```
┌─────────────────────────────────────────────────────────────────┐
│                        MARKET DATA                               │
│   [daily_ohlcv]   [intraday_ohlcv]   [stock_ohlcv]              │
│   [btc_candles]   [top50_stocks]   [financial_statements]        │
│   [btc_alt_data]   [us_momentum_signals]   [disclosures]         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    TRADE EXECUTIONS                              │
│   [btc_trades]   [trade_executions (KR)]   [us_trade_executions] │
│                              ↓                                   │
│                       [trade_snapshots] ── FK → trade_executions │
│                       [execution_quality] ── SmartRouter/Slippage│
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                 PORTFOLIO / RISK STATE                           │
│   [btc_position]   [drawdown_guard_state]   [circuit_breaker_…] │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   ANALYTICS / OBSERVABILITY                      │
│   [signal_ic_history]   [health_snapshots]                       │
│   [daily_reports]   [data_collection_log]                        │
└─────────────────────────────────────────────────────────────────┘
```

**핵심 엔티티 6개**:
1. **Market** — BTC / KR / US 3종
2. **Instrument** — 종목 (stock_code, symbol, 'BTC')
3. **Signal** — 에이전트 매매 판단 (source: rule / ml / llm / composite)
4. **Decision** — Signal을 수용해서 내린 행동 의도 (BUY/SELL/HOLD)
5. **Execution** — 실제 체결 기록
6. **Risk Event** — 드로다운·서킷브레이커·헬스체크

---

## 2. 논리 모델 (정규화 분석)

### 2.1 주요 이상 및 해결 방향

| 범주 | 문제 | 해결 |
|---|---|---|
| **컬럼명 불일치** | `btc_trades.action` vs `trade_executions.trade_type` vs `us_trade_executions.trade_type` | 의미가 같음(BUY/SELL). `btc_trades`는 기존 앱 호환을 위해 컬럼 유지하되 뷰(view)로 `trade_type` 제공 |
| **종목 식별자 혼재** | `stock_code`(KR daily/intraday) vs `ticker`(stock_ohlcv) vs `symbol`(US) | 의미 분리: `stock_code`=KR 6자리, `symbol`=US ticker, 'BTC'는 고정 리터럴. 문서로 명시 + 교차 사용 금지 |
| **JSON 저장 타입 불일치** | `trade_executions.factor_snapshot` text / `us_trade_executions.factor_snapshot` text / `btc_trades.indicator_snapshot` jsonb / `trade_executions.ml_features_json` jsonb | 모두 **jsonb 통일** (쿼리 성능·검증 장점) |
| **NULL 남발** | 거의 모든 수치 컬럼 nullable | 핵심 식별자(`stock_code`, `symbol`, `created_at`)는 NOT NULL, 파생값은 NULL 허용 |
| **타임존 혼재** | 대부분 `timestamptz` / `data_collection_log.timestamp` · `trade_snapshots.timestamp`는 `timestamp without time zone` | **모두 `timestamptz`로 통일** |
| **Signal 출처 추적 불능** | `us_trade_executions.source`만 있고 `trade_executions`·`btc_trades`에 없음 → analyze_with_ai vs rule vs ml 성과 비교 불가 | **모든 체결 테이블에 `signal_source` 컬럼 추가** (CHECK: rule/ml/llm/composite) |
| **Enum 자리 text 중복** | `trade_type`, `result`, `drift_status`, `market_regime`, `status`(position), `trigger_level` 등이 자유 text | **CHECK 제약** 또는 Postgres `enum` 타입 도입. 일단 CHECK 제약으로 최소 변경 |
| **FK 거의 없음** | `trade_snapshots → trade_executions`만. `execution_quality`·`trade_snapshots`는 어느 체결(trade_executions/us_/btc_trades) 참조인지 모호 | `execution_quality`에 `market` 컬럼 이미 있음. 그래도 FK 불가능(3개 테이블 분산) → 논리적 참조 주석 + CHECK 보강 |
| **RLS 부분적** | 6개만 RLS on, 18개는 off | 모든 테이블 RLS on + `service_role` ALL 정책 일괄 (Supabase anon key 노출 시에도 안전) |
| **미사용 테이블** | `trade_snapshots`(0), `data_collection_log`(0), `disclosures`(3), `daily_reports`(36) | 드롭 대신 **archive schema로 이동** 또는 표시 유지하고 주석 추가 (레거시 호환) |
| **stock_ohlcv vs daily_ohlcv 중복** | 두 테이블 모두 일별 OHLCV, 컬럼명·PK만 다름. `stock_ohlcv`는 20 rows뿐 | `stock_ohlcv`는 사실상 deprecated — `daily_ohlcv`로 통합 권고. **본 Phase 범위 밖(코드 광범위 영향)** |
| **signal_ic_history UNIQUE 있지만 active 사용 미명확** | `active` 컬럼 의도 불명 | 유지하되 `COMMENT` 추가 |

### 2.2 3NF 검토 결과
- **1NF**: 대부분 준수 (원자값, 반복 그룹 없음). `factor_snapshot` text에 JSON 직렬화는 스키마-less 확장 필드로 허용 — jsonb로 바꾸면 더 좋음.
- **2NF**: 복합 PK 테이블(`daily_ohlcv` PK=`stock_code,date`, `intraday_ohlcv` UNIQUE=`stock_code,datetime,time_interval`)에서 부분 의존성 없음. OK.
- **3NF**: `financial_statements.stock_name`은 `stock_code`에 함수적 의존. 그러나 종목명이 바뀔 수 있는 시점 스냅샷 성격이라 허용 가능. 주석으로 명시.

### 2.3 정규화 도입 여부
- **완전 3NF 분리는 과도함** — 시계열·스냅샷 테이블이 많아 denormalization이 관용적.
- **Phase 5-E 범위**: 의미론 정정(컬럼명·타입·제약·인덱스·RLS·주석) 중심. 구조적 쪼개기는 보류.

---

## 3. 물리 모델 (최적화)

### 3.1 인덱스 전략

기존 인덱스 + 추가 대상:

| 테이블 | 조회 패턴 | 추가 인덱스 |
|---|---|---|
| `trade_executions` | 최근 체결·종목별 조회 (이미 있음) + `signal_source` 필터 | `idx_trade_executions_source_created_at (signal_source, created_at DESC)` |
| `us_trade_executions` | 동일 + source별 | `idx_us_trade_executions_source_created_at` |
| `btc_trades` | 최근 + action + signal_source | `idx_btc_trades_ts_desc`, `idx_btc_trades_source_ts` |
| `btc_position` | status='OPEN' 빠른 lookup | `idx_btc_position_status` (where status='OPEN') — 부분 인덱스 |
| `health_snapshots` | 52 MB 급증 중 → 파티셔닝 미적용, retention job 필요. 이번 범위엔 미포함 |
| `circuit_breaker_events` | 레벨별 최근 | 이미 `idx_cb_events_level`, `idx_cb_events_created_at` OK |

### 3.2 파티셔닝
- `health_snapshots`(52 MB), `intraday_ohlcv`(15 MB), `btc_trades`(16 MB)는 시계열이라 월별 range 파티셔닝 후보.
- **본 Phase에서는 미적용** — 규모가 아직 크지 않고, 파티셔닝은 대규모 리팩터. Phase 3 (실행·라우팅) 시점으로 미룸.

### 3.3 타임존 정책
- **저장**: 모두 `timestamptz` (UTC internal).
- **애플리케이션 레이어**: KST 변환은 Python `common.config`의 `KST = timezone(timedelta(hours=9))` 사용.
- 이번 마이그레이션에서 `data_collection_log.timestamp`, `trade_snapshots.timestamp`를 `timestamp` → `timestamptz`로 변환 (UTC 가정 캐스팅).

### 3.4 RLS 정책 통일
- 모든 24개 테이블 RLS enable.
- 각 테이블에 `service_role_only_<table>` 정책 단일 (FOR ALL TO service_role USING (true)).
- anon/authenticated 역할은 이 정책 통과 못하므로 완전 차단.

### 3.5 signal_source 확장 (이슈 4 해결 인프라)

신규 컬럼 정의:
```sql
signal_source text CHECK (signal_source IS NULL OR signal_source IN ('rule','ml','llm','composite','manual'))
```

기록 대상:
- `trade_executions.signal_source`
- `us_trade_executions.signal_source` (기존 `source` 컬럼을 이 이름으로 rename — 값 매핑 보존)
- `btc_trades.signal_source`

기록 시점 (Phase 5-E 코드 동기화 단계 E.6에서 구현):
- `btc_trading_agent.execute_buy/sell` 호출 시 신호 결정 로직에 따라 `rule` / `ml` / `llm` / `composite` 세팅
- `stock_trading_agent.execute_buy` 마찬가지
- `us_stock_trading_agent`는 기존 `source` 필드 이미 사용 → rename 반영

---

## 4. 마이그레이션 작업 목록

E.3에서 구현할 ALTER 순서 (원자성 고려):

1. **[비파괴] signal_source 컬럼 추가**
   - `trade_executions` `us_trade_executions` `btc_trades` 각각 ADD COLUMN signal_source text + CHECK
   - `us_trade_executions`는 기존 `source` 데이터를 `signal_source`로 복사 후 `source` DROP은 **다음 단계**로 연기 (하위 호환)

2. **[비파괴] factor_snapshot text → jsonb 변환**
   - `trade_executions.factor_snapshot` 및 `us_trade_executions.factor_snapshot`
   - `ALTER COLUMN factor_snapshot TYPE jsonb USING NULLIF(factor_snapshot,'')::jsonb`
   - 기존 row에 비-JSON text가 있으면 실패 가능 → 사전 체크 쿼리로 검증

3. **[비파괴] 타임존 통일**
   - `data_collection_log.timestamp` timestamp → timestamptz (AT TIME ZONE 'UTC' 캐스팅)
   - `trade_snapshots.timestamp` timestamp → timestamptz

4. **[비파괴] CHECK 제약 추가**
   - `trade_executions.trade_type` IN ('BUY','SELL')  *(NULL 허용 유지)*
   - `us_trade_executions.trade_type` IN ('BUY','SELL')
   - `btc_trades.action` IN ('BUY','SELL','SKIP')  *(기존 SKIP도 저장되는지 확인 후)*
   - `trade_executions.result` IN ('OPEN','CLOSED','STOPPED',NULL 허용)
   - `us_trade_executions.result` IN ('OPEN','CLOSED','STOPPED')
   - `btc_position.status` IN ('OPEN','CLOSED')

5. **[비파괴] NOT NULL 보강**
   - `trade_executions.trade_type` NOT NULL
   - `us_trade_executions.created_at` NOT NULL (이미 default)
   - `btc_trades.price` NOT NULL (이미 default 없지만 실제 쿼리 시 price=NULL은 버그)
   - 실제 NULL row가 있는지 사전 체크 후 안전한 경우만 NOT NULL 적용

6. **[비파괴] 인덱스 추가**
   - 위 3.1 표의 4개 신규 인덱스

7. **[비파괴] RLS 일괄 enable + 정책 적용**
   - 18개 테이블 ENABLE ROW LEVEL SECURITY
   - 각각 service_role ALL 정책

8. **[비파괴] COMMENT ON TABLE/COLUMN 보강**
   - 주요 테이블 역할, signal_source 값 규약, 파편화된 테이블(`trade_snapshots`, `data_collection_log`)의 deprecated 상태 등

### Phase 5-E 범위 **외** (따로 기록)
- `stock_ohlcv` → `daily_ohlcv` 통합
- `disclosures`/`daily_reports` 재설계
- `health_snapshots` retention/파티셔닝
- enum 타입 도입

---

## 5. 적용 방식

- Supabase 브랜치 생성 → 마이그레이션 적용 → 스모크 테스트 (SELECT 1, INSERT DRY-RUN) → merge
- 각 ALTER는 idempotent (IF NOT EXISTS / DO $$ BEGIN ... EXCEPTION ...) 형태로 작성
- 롤백: 각 단계의 inverse를 `supabase/migrations/2026-04-19_phase5e_rollback.sql`에 기록

---

## 6. 변경 비적용 의사결정 로그

| 판단 | 이유 |
|---|---|
| `btc_trades.action` 유지 (rename 안 함) | 코드 10곳에서 `action` 필드 참조. 이름 통일은 애플리케이션 레이어 변경 동반 → 별도 Phase |
| 파티셔닝 연기 | 현재 규모(< 100 MB) 쿼리 성능 이슈 없음. 조기 최적화 회피 |
| enum 타입 미도입 | CHECK 제약으로 충분. enum은 값 변경 시 마이그레이션 무거움 |
| 미사용 테이블 drop 안 함 | 데이터 있음(daily_reports 36, disclosures 3). archive schema 이동은 별도 작업 |
