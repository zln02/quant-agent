---
name: daily-reviewer
description: 장 마감 직후 24h 트레이딩 데이터를 Haiku로 분석해 4섹션 한국어 리뷰 생성 + Telegram 발송 + review_logs persist
model: claude-haiku-4-5-20251001
trigger: cron
entry: scripts/daily_reviewer.py
markets: [KR, US, BTC]
phase: 2 (자가 진단)
---

# Daily Reviewer (PR #28)

PR #12 silence_monitor("무엇이 안 일어났는지") 보완. 매매 결정의 품질·정합·위험을 매일 자가 보고. PR #12 + PR #28 합쳐 자가 보고 layer 완성. Phase 1 (데이터 축적) → Phase 2 (자가 진단) 정식 진입.

## 4 섹션 리포트 구조

1. **매매 요약** — BUY/SELL/HOLD 비율, 실현 손익
2. **알고리즘 정합성** — 룰 vs AI 결정 비율, 신호 다양성, fallback 비율
3. **위험 신호** — drawdown, ML drift PSI, equity 적재 정합
4. **다음 24h 주의 사항** — 위 3섹션 종합 후 LLM 도출

각 섹션 3줄 이내. 데이터 없는 항목 "측정 불가" 명시.

## 실행

- KR 17:00 KST (장 마감 15:30 + 1.5h)
- US 07:00 KST (다음날, NY 17:00 ET)
- BTC 09:00 KST (24h 사이클)

## 데이터 source

| 섹션 | 입력 | 위치 |
|---|---|---|
| 1 (KR) | `trade_executions` 테이블 24h 윈도우 | `created_at` |
| 1 (BTC) | `btc_trades` + `btc_position` (CLOSED) | `timestamp` / `entry_time` |
| 1 (US) | `us_trade_executions` 24h | `created_at` |
| 3 | `brain/risk/latest_snapshot.json` (drawdown), `brain/ml/{kr,us}/drift_report.json` (PSI), `brain/equity/*.jsonl` (mtime) | 파일 read |

BTC PSI는 `brain/ml/btc/drift_report.json` 미존재 → "측정 불가" 표시.

## 토큰 효율

BTC 24h 사이클이 보통 144건 (10분 주기) — 20건 초과 시 `_compress_btc_trades()` 로 summary 압축 (action 분포 + top3 reasons + avg composite_score). KR/US는 24h 0-5건이라 raw dump.

## 출력

- Telegram: `📊 Daily Review [{MARKET}] {KST}` prefix + 4섹션 본문 → `Priority.IMPORTANT` 즉시 발송
- DB: `review_logs` 테이블 INSERT (market, window_end, raw_data JSONB, review_text, model, created_at)

## cron 등록

```cron
# Daily reviewer — PR #28
0 17 * * 1-5 docker exec workspace-kr-agent-1 python /app/scripts/daily_reviewer.py KR >> /home/wlsdud5035/.openclaw/workspace/logs/daily_reviewer.log 2>&1
0 7  * * 2-6 docker exec workspace-us-agent-1 python /app/scripts/daily_reviewer.py US >> /home/wlsdud5035/.openclaw/workspace/logs/daily_reviewer.log 2>&1
0 9  * * *   docker exec workspace-btc-agent-1 python /app/scripts/daily_reviewer.py BTC >> /home/wlsdud5035/.openclaw/workspace/logs/daily_reviewer.log 2>&1
```

## 의존성

- `common.llm_client.call_haiku` (`claude-haiku-4-5-20251001`)
- `common.supabase_client.get_supabase`
- `common.telegram.send_telegram`, `Priority`
- `common.logger.get_logger`
- `common.env_loader.load_env`

## 회귀 영향

매매 사이클과 시간적·코드 의존성 분리. `brain/risk/latest_snapshot.json` 읽기만 (`build_risk_snapshot()` 무거운 호출 X — 기존 `phase18-alert *2 cron`이 갱신). 부하 = Haiku 1회 + Supabase 3-4 SELECT + 1 INSERT + Telegram 1 POST ≈ 벽시계 3-8s, 메모리 50MB 이하.
