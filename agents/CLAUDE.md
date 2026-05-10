# agents/ — AI 전략 에이전트 모음

5-에이전트 팀 폐기 (2026-05-10) 후 단독 실행 모듈 + 헬스체크/알림 보조 모듈로 구성.

## 보존 모듈

### 핵심 가이드 (4개)
| 모듈 | 역할 | 실행 |
|------|------|------|
| `regime_classifier.py` | 시장 국면 분류 (BULL/BEAR/SIDEWAYS/CRISIS) | btc/kr/us 거래 사이클에서 직접 호출 |
| `alert_manager.py` | 텔레그램 경보 발송 관리 | 임계 위반 시 호출 |
| `daily_report.py` | 일간 리포트 생성 | cron 일간 실행 |
| `weekly_report.py` | 주간 리포트 생성 | cron 주간 실행 |

### 보조 모듈
- `gateway_agent.py` — 텔레그램 자연어 인터페이스 (`/ask` `/market` `/review`에서 import)
- `daily_loss_analyzer.py` — 일일 손실 분석 (텔봇 `/daily_loss`)
- `self_healer.py` — 5분 cron 헬스체크 (Docker/DB/메모리/로그)
- `README.md` — 모듈 변경 시 동기화

## 절대 규칙
- 모듈 추가/수정 시 `agents/README.md` 반드시 동기화
- 모델 변경 시 `common/config.py` MODEL_* 상수 통해서만 수정
- `self_healer.py` cron 5분 주기 — 임의 비활성화 금지
- `regime_classifier.py` CRISIS 모드 — 모든 거래 파라미터 오버라이드

## 레짐 분류 (regime_classifier.py)
| 레짐 | 의미 | 거래 영향 |
|------|------|-----------|
| BULL | 상승장 | momentum 가중치 ↑ |
| BEAR | 하락장 | 포지션 축소 |
| SIDEWAYS | 횡보 | 평균회귀 전략 |
| CRISIS | 위기 | 전체 거래 중단 |

## 폐기 기록 (2026-05-10)

5-에이전트 팀 + 보조 모듈 폐기:
- `trading_agent_team.py` — Orchestrator (opus-4-6)
- `news_analyst.py` — 뉴스 감성 (haiku-4-5)
- `strategy_reviewer.py` — 일간 전략 경량 리뷰
- `conflict_resolver.py` — 에이전트 간 신호 충돌 해소
- `agent_performance.py` — 에이전트별 성과 추적
- `decision_logger.py` — Supabase `agent_decisions` 로그 (5-agent 외 사용처 없음)

폐기 이유: 1+달 의도적 미운영 후 ML-only 메인 사이클(TFT+Autoencoder+factor) 정상 동작 확인. 고빈도 트레이딩에 LLM 800-2400ms 지연 부적합.

폐기 기록: 추후 `docs/AGENTS_DECOMM.md` (Step 7) 참조.
Commit 추적: `git log --grep='5-agent'`

부활 참조: `TauricResearch/TradingAgents` 패턴, 별도 regime-shift 전용 모듈 도입 시 재검토.
