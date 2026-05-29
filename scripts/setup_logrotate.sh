#!/usr/bin/env bash
# OpenClaw logs/ retention 정책 적용 스크립트 (PR #27).
# 7일 + gzip + 30일 후 삭제. host 측 cron + logrotate(8) 활용.
#
# 실행: sudo bash scripts/setup_logrotate.sh
# 검증: sudo logrotate -d /etc/logrotate.d/openclaw

set -euo pipefail

LOG_DIR="/home/wlsdud5035/.openclaw/logs"
CONF="/etc/logrotate.d/openclaw"

if [ "$EUID" -ne 0 ]; then
    echo "이 스크립트는 root 권한 필요 (sudo). conf 미리보기만 출력:"
    DRY=1
fi

if [ ! -d "$LOG_DIR" ]; then
    echo "ERROR: $LOG_DIR 미존재 — 경로 확인"
    exit 1
fi

cat <<EOF | (${DRY:+cat} ${DRY:-tee "$CONF"})
$LOG_DIR/*.log {
    daily
    rotate 7
    maxage 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0640 wlsdud5035 wlsdud5035
    su wlsdud5035 wlsdud5035
}
EOF

if [ -n "${DRY:-}" ]; then
    echo ""
    echo "위 내용을 $CONF 에 저장하려면 sudo로 실행:"
    echo "  sudo bash $0"
    exit 0
fi

chmod 644 "$CONF"
chown root:root "$CONF"

echo "logrotate 설정 적용: $CONF"
echo "검증: sudo logrotate -d $CONF"
echo "수동 실행: sudo logrotate -f $CONF"
echo ""
echo "예상 효과:"
echo "  - 일별 회전, 7일치 보관 (현 105MB → ~15MB)"
echo "  - 30일 후 자동 삭제"
echo "  - gzip 압축 (PR #26 권한 640 유지)"
