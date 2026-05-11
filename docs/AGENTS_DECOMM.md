# 5-Agent Team Decommission Record

**Date**: 2026-05-10
**Branch**: feature/remove-agent-team
**Commits**: e01508379 → e82899959 (Step 2~6, see `git log --grep='5-agent'`)

## 폐기 대상

### 코드
- `agents/trading_agent_team.py` — Orchestrator (anthropic, opus-4-6)
- `agents/news_analyst.py` — 뉴스 감성 (haiku-4-5)
- `agents/strategy_reviewer.py` — 일간 전략 리뷰
- `agents/conflict_resolver.py` — 에이전트 간 신호 충돌 해소
- `agents/agent_performance.py` — 에이전트별 성과 추적
- `agents/decision_logger.py` — Supabase `agent_decisions` 로그
- `company/` — CEO-CTO 위임 구조 (OpenClaw AI 회사 시뮬)
- `scripts/run_agent_team.sh`, `scripts/run_company.sh`

### UI/API
- `/api/agents/decisions`, `/api/agent-decisions`, `/api/decisions/{market}`, `/api/agent-performance`
- dashboard `AgentsPage.jsx`, `AgentsPanel.jsx`
- `telegram_bot.py`: `/why`, `/agents`, `/performance` 명령 + 관련 함수 6개

### 보존 모듈에서 잔존 코드 정리
- `agents/weekly_report.py` — `strategy_reviewer` 통합 부분 제거

## 폐기 이유

1. **1+달 의도적 미운영** 후 ML-only 메인 사이클 정상 동작 확인
   - cron 등록 0
   - 외부 호출처 0 (audit 검증)
   - BTC 메인 사이클 OpenAI 호출 0건 (24h 컨테이너 로그)

2. **고빈도 트레이딩 부적합**
   - LLM API 지연 800-2400ms vs 인디케이터 마이크로초
   - 통계적 edge 파괴 (binary sentiment 필터)

3. **운영 복잡도 vs 알파**
   - 5-agent 팀 24KB+ 코드 유지보수 비용
   - 학술 multi-agent 우위 주장 vs 실증 robust 성능 격차
     (LLM Multi-Agent Survey 2026)

## 부활 트리거 (메모)

- **거시 regime shift 감지** 별도 모듈로 (트레이딩 의사결정 X)
- **저빈도 포트폴리오 리밸런싱** 도입 시
- **이벤트 기반 알파** (FOMC, 실적 발표) 추구 시

## 부활 시 참고

- TauricResearch/TradingAgents 패턴 (Bull/Bear/Risk Supervisor)
- BlackRock 2026 Three-Layer Multi-Agent Framework
- 본 폐기 commit revert 가능: `git revert e82899959..e01508379`

## DB 정리 별개 작업

`agent_decisions`, `agent_performance` 테이블 자체 drop은 Step 8 또는
Supabase 콘솔에서 별도 SQL 실행. 본 PR 머지로 코드 호출자 0 → drop 안전.
