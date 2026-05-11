# quant-agent — 개인 자동매매 플랫폼

@btc/CLAUDE.md
@agents/CLAUDE.md
@quant/CLAUDE.md
@execution/CLAUDE.md

BTC(Upbit 실거래) / KR(Kiwoom 모의) / US(yfinance 시뮬) 3시장 자동매매. ML 기반 신호 파이프라인 + AI 전략 보조 모듈(레짐 분류 · 리포트 · 알림).

## 절대 규칙
- **`btc/` 실거래 코드 수정 시 반드시 Plan Mode 먼저**
- `.env` 파일 절대 커밋 금지
- `DrawdownGuard` / `CircuitBreaker` 변경 시 테스트 필수
- `/sell_all` 명령 확인 단계 제거 금지

---

## Commands

### Development
```bash
# Activate virtualenv
source .venv/bin/activate

# Run agents directly
python btc/btc_trading_agent.py           # BTC (Upbit live trading)
python stocks/stock_trading_agent.py      # KR stocks (Kiwoom paper)
python stocks/us_stock_trading_agent.py   # US stocks (yfinance dry-run)
python btc/btc_dashboard.py               # FastAPI dashboard at :8080

# Tests
pytest                                    # Run all tests
pytest tests/test_metrics.py             # Single test file
pytest -k "test_calc"                    # Filter by name
```

### Docker (production)
```bash
# Launch all 7 services
docker compose up -d

# Frontend changes require rebuild
docker compose build dashboard && docker compose up -d dashboard

# Env setup (first time)
cp btc/.env.example .env
bash scripts/split_docker_env.sh .env    # generates .env.runtime + .docker-secrets/
```

### Research loop (cron)
```
Saturday  22:00 → quant/alpha_researcher.py → brain/alpha/best_params.json
Sunday    23:00 → quant/signal_evaluator.py → brain/alpha/weights.json
Sunday    23:30 → quant/param_optimizer.py  → applies params + Telegram report
Daily     08:30 → stocks/ml_model.py retrain (if ≥50 trades)
```

## Architecture

**7 Docker services**: `dashboard` (FastAPI + React SPA, :8080), `btc-agent` (600s loop), `kr-agent` (600s loop), `us-agent` (900s loop), `telegram-bot`, `prometheus` (:9090), `grafana` (:3000)

**OpenClaw Gateway**: AI 비서 `제이(J)` 통합 레이어. 텔레그램과 연결되고, 20개 스킬 묶음(`trading-ops`, `market-briefing`, `signal-query`, `trade-executor` 등)을 사용한다. Gateway port는 `18789`.

**Backend** (`btc/btc_dashboard.py`): FastAPI mounts three route groups:
- `btc/routes/btc_api.py` → `/api/btc/*`, `/api/candles`, `/api/trades`, etc.
- `btc/routes/stock_api.py` → `/api/kr/*`, `/api/stocks/*`
- `btc/routes/us_api.py` → `/api/us/*`
- All routes protected via `dependencies=[Depends(_require_auth)]` except `/health`

**Frontend** (`dashboard/`): React + Vite SPA built to `dashboard/dist/`, served as static. Uses `usePolling` hook for 30–300s polling (no WebSocket). Central API client in `dashboard/src/api.js`.

**Signal pipeline**:
- BTC: 10-factor composite score (0–100) → regime filter → Upbit live order
- KR: rule-based 60% + XGBoost ML 40% blend → Kiwoom paper order
- US: multi-factor momentum ranking → regime gate → dry-run log only

**AI agents** (`agents/`): 전략 보조 모듈 — `regime_classifier.py`(BULL/BEAR/SIDEWAYS/CRISIS 분류, 매 사이클), `alert_manager.py`(임계 위반 시 텔레그램 경보), `daily_report.py` / `weekly_report.py`(cron 일/주간 리포트). 5-에이전트 팀 폐기 (2026-05-10) — 상세는 [docs/AGENTS_DECOMM.md](docs/AGENTS_DECOMM.md).

**Gateway Agent** (`agents/gateway_agent.py`): 텔레그램 자연어 인터페이스 보조 계층. `stocks/telegram_bot.py`의 `/ask`, `/market`, `/review`에서 직접 import되어 현재도 사용 중이다.

**Level 5 Research loop** (`quant/`): weekly automated IC/IR evaluation → parameter auto-tuning → live agent feedback.

**Risk Management Layer**:

- `quant/risk/drawdown_guard.py` — 3-tier drawdown rules (daily/weekly/monthly)
- `quant/risk/position_sizer.py` — Kelly fractional sizing + ATR volatility adjustment
- `quant/risk/correlation_monitor.py` — Cross-market concentration risk detection
- `common/circuit_breaker.py` — Auto-recovery circuit breaker with file-based cooldown

**Monitoring**:

- `agents/self_healer.py` — 5min cron: dashboard, log freshness, disk, Docker, memory, Supabase, correlation
- `prometheus.yml` — 3 scrape jobs: dashboard, node-exporter, agent metrics

## Code Rules

1. Wrap all external API calls in `try/except` — Upbit, Kiwoom, OpenAI/Claude, Supabase, Telegram, Notion
2. Load env vars only via `common/env_loader.py load_env()` — never `os.environ.get()` directly
3. Use `common/logger.py get_logger()` — no `print()` in production code
4. Hardcode nothing: stop-loss/take-profit ratios, model names, sleep intervals, cooldowns, ports → all in `common/config.py`
5. Add type hints to all public functions (return type required)
6. Use `common/retry.py retry_call()` for network calls
7. Use `common/api_utils.py` `api_success()`/`api_error()` for all FastAPI route responses
8. All config values must come from `common/config.py` — never hardcode thresholds, ratios, timeouts

## Risk Files — Edit with Extreme Care

| File | Risk |
|------|------|
| `stocks/kiwoom_client.py place_order()` | Real orders to Kiwoom |
| `btc/btc_trading_agent.py` buy/sell logic | Upbit live trades |
| `quant/risk/drawdown_guard.py` | Never disable |
| `stocks/telegram_bot.py /sell_all` | Never remove confirmation step |
| `agents/regime_classifier.py` CRISIS mode | Overrides all trade params |
| `common/supabase_client.py` | Schema changes need migration |

## Key Paths

- `brain/` — AI analysis outputs (params, weights, regime state)
- `memory/` — long-term agent memory
- `.docker-secrets/` — secrets mounted at `/run/local-secrets/` in containers
- `.env.runtime` — generated from `.env` by `split_docker_env.sh`

## Dashboard Frontend

Pages: `BtcPage`, `KrStockPage`, `UsStockPage` — all in `dashboard/src/pages/`

Chart timeframes:
- BTC: `minute5/minute10/minute60/week/month/day` (pyupbit intervals)
- KR: `5m/1h/1d` with variable `limit` (Supabase `intraday_ohlcv` / `daily_ohlcv`)
- US: `5d/1mo/3mo/6mo/1y/5y` with `interval` (yfinance periods)

`usePolling(fn, intervalMs, [deps])` — re-fetches when deps change (e.g. selected timeframe/symbol).

## Design Context (Dashboard)

> 전체 내용: `.impeccable.md` 참조

**사용 맥락**: 사후 분석·리뷰 (거래 후 에이전트 결정 검토)
**방향**: 깔끔한 모던 대시보드 — 카드 중심, 큰 숫자, 충분한 여백

**5 Design Principles**:
1. **계층 명확화** — 종합 점수 > 가격/PnL > 개별 신호 > 로그 순 시각 무게
2. **차트 우선** — 캔들스틱에 최대 공간 (사후 분석 목적)
3. **정보 축소** — 핵심만 노출, 부가 정보는 탭/접기
4. **에이전트 로그 가독성** — BUY/SELL/HOLD + 이유 타임라인으로
5. **조용한 색상** — teal(이익)·red(손실)·btc-orange(점수) 3색만 강조
