# Agents Module

AI 전략 에이전트 계층. 시장 레짐 분류, 알림, 정기 리포트를 담당한다.

5-에이전트 팀 폐기 (2026-05-10) — 자세한 내역은 [docs/AGENTS_DECOMM.md](../docs/AGENTS_DECOMM.md) 참조.

## 파일 구조

| 파일 | 역할 | 사용 빈도 |
|------|------|----------|
| regime_classifier.py | 시장 레짐 분류 (BULL/BEAR/SIDEWAYS/CRISIS) | 매 사이클 |
| alert_manager.py | 리스크 알림 (드로우다운, 손실 한도) | 임계 위반 시 |
| daily_report.py | 일간 리포트 생성 | cron 일간 |
| weekly_report.py | 주간 리포트 생성 | cron 주간 |
| daily_loss_analyzer.py | 일일 손익 분석 | 텔봇 `/daily_loss` |
| gateway_agent.py | 텔레그램 자연어 인터페이스 (`/ask` `/market` `/review`) | 텔레그램 요청 시 |
| self_healer.py | 시스템 헬스체크 (Docker/DB/메모리/로그) | cron 5분 |

## 의존성

- `common/logger.py` — 로깅
- `common/supabase_client.py` — DB 접근
- `common/telegram.py` — 알림 발송
- `common/config.py` — 설정값

## 실행 예시

```bash
# 레짐 분류 단독 실행
python -m agents.regime_classifier

# 헬스체크 (cron 5분 주기)
python agents/self_healer.py
```
