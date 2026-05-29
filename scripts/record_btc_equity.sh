#!/usr/bin/env bash
# BTC equity 스냅샷 기록 (Docker 에이전트 fallback)
# Docker btc-agent가 정상이면 이미 equity가 기록됨.
# 이 스크립트는 Docker가 다운됐을 때 또는 docker가 살아있어도 equity 적재가
# 6시간 이상 정체된 경우 호스트에서 직접 잔고를 기록하는 안전망.
# crontab: */10 * * * * /home/wlsdud5035/quant-agent/scripts/record_btc_equity.sh
set -euo pipefail

source "$(dirname "$0")/load_env.sh"
load_openclaw_env
export PYTHONPATH="${WORKSPACE}:${PYTHONPATH:-}"

PYTHON_BIN="${WORKSPACE}/.venv/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

# PR #24: docker btc-agent가 살아있어도 equity stale 6h+ 면 host에서 기록.
# (실제 사례: docker 컨테이너 사이클은 도는데 TypeError 등으로 equity 적재만 실패)
EQUITY_FILE="${WORKSPACE}/brain/equity/btc.jsonl"
STALE_THRESHOLD_S=21600  # 6h
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "btc-agent"; then
    if [ -f "$EQUITY_FILE" ]; then
        AGE_S=$(( $(date +%s) - $(stat -c %Y "$EQUITY_FILE") ))
        if [ "$AGE_S" -lt "$STALE_THRESHOLD_S" ]; then
            exit 0
        fi
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker btc-agent 실행중이지만 equity ${AGE_S}s stale — host에서 기록"
    fi
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker btc-agent 미실행 — 호스트에서 equity 기록"
fi
"$PYTHON_BIN" -c "
from common.env_loader import load_env
load_env()
import pyupbit, os
ak = os.environ.get('UPBIT_ACCESS_KEY','')
sk = os.environ.get('UPBIT_SECRET_KEY','')
if not ak or not sk:
    print('Upbit 키 없음, 스킵')
    exit(0)
upbit = pyupbit.Upbit(ak, sk)
krw = float(upbit.get_balance('KRW') or 0)
btc = float(upbit.get_balance('BTC') or 0)
price = float(pyupbit.get_current_price('KRW-BTC') or 0)
eq = krw + btc * price
if eq > 0:
    from common.equity_loader import append_equity_snapshot
    append_equity_snapshot('btc', eq, {'source': 'host_fallback', 'price': price})
    print(f'기록 완료: {eq:,.0f} KRW')
else:
    print('equity=0, 스킵')
"
