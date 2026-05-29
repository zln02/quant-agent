# US 실거래 진입 가이드 (PR #24)

OpenClaw US 매매 에이전트(`stocks/us_stock_trading_agent.py`)는 `US_TRADING_ENV`
환경변수로 3가지 모드를 지원한다.

| 모드 | 설명 | equity source |
|------|------|---------------|
| `sim` | (기본값) virtual_capital $10,000 시뮬레이션. Alpaca 미연결 | `virtual_capital` |
| `paper` | Alpaca paper 계정으로 가상 주문. 실제 체결가/슬리피지/수수료 검증 | `alpaca_paper` |
| `live` | Alpaca 실거래 계정. 실제 자본 투입 | `alpaca_live` |

## 0. 현재 상태 점검

```bash
docker exec workspace-us-agent-1 env | grep US_TRADING_ENV
# 출력 없거나 sim → 시뮬레이션 모드
```

`tail -1 brain/equity/us.jsonl` 의 `metadata.source` 가 `virtual_capital` 이면 sim.

## 1. Alpaca paper 진입 (1주 검증)

### 1.1 키 발급
1. https://alpaca.markets 회원가입
2. Dashboard → Paper Trading → API Keys → Generate
3. `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` 복사

### 1.2 .env 설정
`/home/wlsdud5035/.openclaw/workspace/.env` 에 추가:

```bash
US_TRADING_ENV=paper
ALPACA_API_KEY=PK...           # paper key
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

### 1.3 재시작
```bash
cd /home/wlsdud5035/.openclaw/workspace
docker compose restart us-agent
docker logs workspace-us-agent-1 --tail 50 | grep -E "US_TRADING_ENV|paper"
# "📄 US_TRADING_ENV=paper" 로그 확인
```

### 1.4 검증 (1주)
- `us_trade_executions` row가 paper 주문으로 발생하는지: Supabase 콘솔 확인
- `brain/equity/us.jsonl` 의 `metadata.source = "alpaca_paper"` 인지
- 백테스트와 실 슬리피지/수수료 비교:
  ```bash
  python scripts/paper_live_diff.py  # PR #25에서 추가 예정
  ```

## 2. live 전환 (paper 1주 안정 확인 후)

### 2.1 키 재발급
Alpaca Dashboard → **Live Trading** 탭에서 별도로 발급 (paper와 분리).

### 2.2 .env 변경
```bash
US_TRADING_ENV=live
ALPACA_API_KEY=...             # LIVE key (paper key와 다름)
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://api.alpaca.markets
```

### 2.3 재시작 + 즉시 점검
```bash
docker compose restart us-agent
# 5분 대기 후
docker logs workspace-us-agent-1 --since 5m | grep -E "WARN|ERROR|💰"
```

`"💰 US_TRADING_ENV=live — Alpaca 실거래 모드"` 가 보이면 진입 완료.

## 3. 롤백 (문제 발생 시)

```bash
# .env 에서
US_TRADING_ENV=sim

docker compose restart us-agent
```

기존 sim 모드로 즉시 복귀. 단, live에서 보유한 open position은 자동 청산되지 않으므로
Alpaca 웹에서 수동 정리 또는 us-agent 자체에서 청산 사이클 완료까지 대기.

## 4. 모니터링

- `silence_monitor.py` 가 24h+ equity stale 자동 감지 (PR #24)
- 자비스 알림: 사이클 시작 시 모드 라벨 자동 포스팅 (PR #23 jay_bridge 연동)
- Prometheus: `us_trade_executions` count, equity gauge

## 5. 자본 규모 가이드

| 단계 | 자본 | 비고 |
|------|------|------|
| paper | 가상 $10K | 1주 무중단 |
| live (소액) | $1,000~$3,000 | 1~2주, max_position_pct=2% |
| live (정상) | $10K+ | RISK 설정 조정 (`stocks/us_stock_trading_agent.py` `RISK` dict) |

`RISK["virtual_capital"]` 은 sim 모드 자본 가상치. live 모드에서는 Alpaca 계좌
실잔고가 정답이며, `virtual_capital` 값은 reference로만 사용됨.

## 6. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `Alpaca 매수 실패: insufficient_balance` | live 계좌 잔고 부족 | Alpaca 입금 또는 paper 복귀 |
| `ALPACA_API_KEY missing` | .env 미설정 | 1.2 참조 |
| `403 Forbidden` | live/paper 키 혼용 또는 BASE_URL 불일치 | 1.2/2.2 BASE_URL 매칭 확인 |
| equity stale 알림 | Alpaca API 401 또는 사이클 차단 | `docker logs workspace-us-agent-1` 점검 |
