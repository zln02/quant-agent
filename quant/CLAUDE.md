# quant/ — 퀀트 엔진

백테스트 · 팩터 · 포트폴리오 · 리스크 · 주간 자동 파라미터 최적화 루프.

## 주간 자동화 루프
```
토요일 22:00  alpha_researcher.py   → brain/alpha/best_params.json  (IC/IR 그리드서치)
일요일 23:00  signal_evaluator.py   → brain/alpha/weights.json       (신호 IC/IR 측정)
일요일 23:30  param_optimizer.py    → 파라미터 자동 반영 + 텔레그램 리포트
평일   08:30  stocks/ml_model.py retrain (체결 50건 이상 시)
```

> ⚠️ **현재 콜드스타트 상태** — `trade_executions=38` (2026-05) < 50. KR 실거래 시작 후 50건 도달 시 ML 자동 활성화. 콜드스타트 동작은 `tests/test_stock_signal.py` 4개 분기 테스트 참조.

## 파일 구조
```
quant/
├── alpha_researcher.py    # 룰기반 파라미터 그리드서치 (IC/IR 최적화)
├── signal_evaluator.py    # 신호 IC/IR 측정 → Supabase 저장
├── param_optimizer.py     # attribution 기반 파라미터 자동 반영
├── cross_market_risk.py   # 시장 간 상관 리스크 모니터링
├── backtest/
│   ├── engine.py          # 백테스트 엔진 (벡터화)
│   └── universe.py        # 유니버스 정의 (KR/US/BTC)
├── factors/
│   ├── analyzer.py        # 팩터 분석 (IC, ICIR, 분포)
│   ├── combiner.py        # 팩터 합산 (가중 평균)
│   └── registry.py        # 팩터 등록 / 목록 관리
├── portfolio/
│   ├── optimizer.py       # 포트폴리오 최적화 (MVO/리스크 패리티)
│   ├── rebalancer.py      # 리밸런싱 실행
│   ├── attribution.py     # 팩터 PnL 귀속 (WeeklyAttributionRunner)
│   └── cross_market_manager.py  # 크로스 마켓 포지션 관리
└── risk/
    ├── drawdown_guard.py  # 3단계 드로우다운 룰 ⚠️ 비활성화 금지
    ├── position_sizer.py  # Kelly 분수 + ATR 사이징
    ├── var_model.py       # VaR 계산 (95/99%)
    ├── exposure.py        # 노출도 측정
    ├── correlation.py     # 상관계수 계산
    └── correlation_monitor.py  # 집중 리스크 실시간 탐지
```

## 파라미터 로드 패턴
```python
# 에이전트 시작 시 brain/agent_params.json 로드
from quant.param_optimizer import load_best_params
params = load_best_params()  # 없으면 config.py 기본값 사용
```

## 리스크 파일
| 파일 | 이유 |
|------|------|
| `risk/drawdown_guard.py` | 비활성화 시 손실 제한 없어짐 |
| `risk/position_sizer.py` | 사이징 오류 = 과다 포지션 |
| `param_optimizer.py` | 실제 거래 파라미터 자동 변경 |

## 테스트
```bash
cd ~/quant-agent
pytest tests/ -k "quant or backtest or factor" -v
```
