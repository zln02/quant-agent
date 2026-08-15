"""
self_healer.py — OpenClaw 자동 자가진단·수복 에이전트
==========================================================
실행: python agents/self_healer.py [--dry-run] [--quiet]

기능:
1. 헬스체크 실패 감지 → 자동 복구 시도
2. Google Sheets OAuth 만료 → 텔레그램 재인증 안내
3. equity 기록 중단 → 호스트 equity recorder 재시작
4. Supabase 연결 실패 → 재연결 시도
5. 대시보드 다운 → 재시작 시도
6. 복구 결과를 텔레그램으로 보고

crontab: */5 * * * * /home/wlsdud5035/openclaw/scripts/run_self_healer.sh
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.env_loader import load_env
from common.logger import get_logger

load_env()
log = get_logger("self_healer")

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", Path.home() / ".openclaw"))
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", OPENCLAW_ROOT / "workspace"))
OPENCLAW_OLD = Path(os.environ.get("OPENCLAW_OLD_DIR", Path.home() / "openclaw"))
LOG_DIR = Path(os.environ.get("OPENCLAW_LOG_DIR", OPENCLAW_ROOT / "logs"))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
STALE_MINUTES = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _send_telegram(msg: str) -> None:
    try:
        from common.telegram import send_telegram
        send_telegram(msg)
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


def _run(cmd: str, timeout: int = 15) -> tuple[int, str]:
    """Shell 명령 실행, (returncode, output) 반환"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:
        return 1, str(e)


# ── 1. 헬스 상태 파일 읽기 ──────────────────────────────────────────────
def load_health_status() -> dict[str, Any]:
    status_file = LOG_DIR / "health_status.json"
    if not status_file.exists():
        return {}
    try:
        import json
        return json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 2. Supabase 연결 테스트 ──────────────────────────────────────────────
def check_supabase() -> tuple[bool, str]:
    try:
        from common.supabase_client import get_supabase, run_query_with_retry
        sb = get_supabase()
        if not sb:
            return False, "Supabase 클라이언트 없음"
        result = run_query_with_retry(
            lambda c: c.table("btc_position").select("id").limit(1).execute()
        )
        if result is None:
            return False, "Supabase 응답 없음"
        return True, "OK"
    except Exception as e:
        return False, str(e)


# ── 3. 대시보드 HTTP 체크 ────────────────────────────────────────────────
def check_dashboard() -> tuple[bool, str]:
    try:
        import socket
        with socket.create_connection(("127.0.0.1", DASHBOARD_PORT), timeout=3):
            return True, f"port {DASHBOARD_PORT} OK"
    except Exception as e:
        return False, str(e)


# ── 4. equity 기록 신선도 체크 ──────────────────────────────────────────
def check_equity_freshness() -> dict[str, Any]:
    issues = {}
    equity_dir = WORKSPACE / "brain" / "equity"
    # symlink 따라가기
    if equity_dir.is_symlink():
        equity_dir = equity_dir.resolve()

    now = time.time()
    weekday = _utc_now().weekday()  # 0=월 … 6=일
    kst_hour = (_utc_now().hour + 9) % 24

    for market in ("btc", "kr"):
        path = equity_dir / f"{market}.jsonl"

        # KR: 평일 장중(09:00~15:30 KST)에만 체크
        if market == "kr":
            if weekday >= 5:  # 토·일 skip
                continue
            if not (9 <= kst_hour < 16):  # 장외 skip
                continue

        if not path.exists():
            issues[market] = f"{market}.jsonl 없음"
            continue
        age_hours = (now - path.stat().st_mtime) / 3600
        # BTC: 4시간, KR: 1시간
        threshold = 1.0 if market == "kr" else 4.0
        if age_hours > threshold:
            issues[market] = f"{age_hours:.1f}시간째 미갱신"
    return issues


# ── 5. Google Sheets OAuth 상태 체크 ────────────────────────────────────
def check_sheets_oauth() -> tuple[bool, str]:
    gog = WORKSPACE / "gog-docker"
    if not gog.exists():
        return True, "gog 없음(skip)"
    password = os.environ.get("GOG_KEYRING_PASSWORD", "")
    if not password:
        return True, "GOG_KEYRING_PASSWORD 없음(skip)"
    account = os.environ.get("GOG_ACCOUNT", "")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not account or not sheet_id:
        return True, "GOG_ACCOUNT / GOOGLE_SHEET_ID 없음(skip)"
    rc, out = _run(
        f'GOG_KEYRING_PASSWORD="{password}" {gog} sheets read '
        f'--account "{account}" '
        f'"{sheet_id}" "시트1!A1:A1" 2>&1',
        timeout=15,
    )
    if "invalid_grant" in out or "Bad Request" in out:
        return False, "OAuth token 만료 (invalid_grant)"
    return rc == 0, out[:100] if rc != 0 else "OK"


# ── 6. BTC equity recorder 재시작 ────────────────────────────────────────
def restart_btc_equity_recorder() -> bool:
    script = OPENCLAW_OLD / "scripts" / "record_btc_equity.sh"
    if not script.exists():
        return False
    if DRY_RUN:
        log.info(f"[DRY-RUN] would run: {script}")
        return True
    rc, out = _run(f"bash {script}", timeout=30)
    if rc == 0:
        log.info(f"BTC equity 기록 복구: {out[:80]}")
        return True
    log.warning(f"BTC equity recorder 실패: {out[:100]}")
    return False


# 시간당 재시작 횟수 제한 (단순 파일 기반 카운터)
_RESTART_STATE = WORKSPACE / ".cache" / "self_healer_restarts.json"
_RESTART_MAX_PER_HOUR = 3
_SERVICE_MAP = {
    "btc": "workspace-btc-agent-1",
    "kr": "workspace-kr-agent-1",
    "us": "workspace-us-agent-1",
    "dashboard": "workspace-dashboard-1",
    "telegram": "workspace-telegram-bot-1",
}


def _restart_count_in_last_hour(service: str) -> int:
    import json as _json
    try:
        data = _json.loads(_RESTART_STATE.read_text()) if _RESTART_STATE.exists() else {}
    except Exception:
        return 0
    cutoff = time.time() - 3600
    return sum(1 for ts in data.get(service, []) if ts > cutoff)


def _record_restart(service: str) -> None:
    import json as _json
    _RESTART_STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = _json.loads(_RESTART_STATE.read_text()) if _RESTART_STATE.exists() else {}
    except Exception:
        data = {}
    data.setdefault(service, []).append(time.time())
    cutoff = time.time() - 7200
    data[service] = [ts for ts in data[service] if ts > cutoff]
    _RESTART_STATE.write_text(_json.dumps(data))


def restart_container(service: str) -> tuple[bool, str]:
    """Docker 컨테이너 재시작 (시간당 {_RESTART_MAX_PER_HOUR}회 제한)."""
    container = _SERVICE_MAP.get(service)
    if not container:
        return False, f"unknown service: {service}"
    recent = _restart_count_in_last_hour(service)
    if recent >= _RESTART_MAX_PER_HOUR:
        return False, f"rate-limited ({recent}/{_RESTART_MAX_PER_HOUR} in last 1h)"
    if DRY_RUN:
        log.info(f"[DRY-RUN] would restart: {container}")
        return True, "dry-run"
    rc, out = _run(f"docker restart {container}", timeout=30)
    if rc == 0:
        _record_restart(service)
        log.info(f"컨테이너 재시작 완료: {container}")
        return True, out.strip()[:80]
    log.warning(f"컨테이너 재시작 실패: {container} | {out[:100]}")
    return False, out.strip()[:100]


# ── 메인 진단 루프 ─────────────────────────────────────────────────────────
def run_self_healing(quiet: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {
        "timestamp": _utc_now().isoformat(),
        "actions": [],
        "alerts": [],
    }

    def _ok(msg: str) -> None:
        if not quiet:
            log.info(msg)

    def _action(msg: str) -> None:
        log.info(f"[ACTION] {msg}")
        report["actions"].append(msg)

    def _alert(msg: str) -> None:
        log.warning(f"[ALERT] {msg}")
        report["alerts"].append(msg)

    # --- 1. Supabase -------------------------------------------------------
    sb_ok, sb_msg = check_supabase()
    if sb_ok:
        _ok(f"Supabase: {sb_msg}")
    else:
        _alert(f"Supabase 연결 실패: {sb_msg}")
        # 재연결은 get_supabase() 자체 retry에 맡김

    # --- 2. 대시보드 -------------------------------------------------------
    dash_ok, dash_msg = check_dashboard()
    if dash_ok:
        _ok(f"대시보드: {dash_msg}")
    else:
        _alert(f"대시보드 응답 없음: {dash_msg}")

    # --- 3. equity 신선도 --------------------------------------------------
    equity_issues = check_equity_freshness()
    for market, issue in equity_issues.items():
        _alert(f"equity {market}: {issue}")
        if market == "btc":
            _action("BTC equity recorder 재실행 중")
            ok = restart_btc_equity_recorder()
            if ok:
                _action("BTC equity 기록 복구 완료")
            else:
                _alert("BTC equity recorder 재시작 실패 — 수동 확인 필요")

    # --- 4. Google Sheets OAuth -------------------------------------------
    oauth_ok, oauth_msg = check_sheets_oauth()
    if oauth_ok:
        _ok(f"Google Sheets OAuth: {oauth_msg}")
    else:
        _alert(f"Google Sheets OAuth 만료: {oauth_msg}")
        _action("Sheets OAuth 재인증 안내 전송")
        if not DRY_RUN:
            _send_telegram(
                "🔑 <b>[Self-Healer] Google Sheets OAuth 만료</b>\n"
                "재인증이 필요합니다.\n\n"
                "<b>재인증 방법:</b>\n"
                "터미널에서 실행:\n"
                '<code>GOG_KEYRING_PASSWORD="$GOG_KEYRING_PASSWORD" '
                "~/.openclaw/workspace/gog-docker auth login "
                '--account "$GOG_ACCOUNT"</code>\n\n'
                "브라우저에서 Google 로그인 완료 후 자동으로 복구됩니다."
            )

    # --- 5. drawdown 이상 알림 + 자동 컨테이너 재시작 ---------------------
    health = load_health_status()
    agents = health.get("agents", {})
    for agent_name, status in agents.items():
        if status not in ("ok", "SKIPPED") and status:
            try:
                stale_min = int(status)
                if stale_min > STALE_MINUTES:
                    _alert(f"{agent_name.upper()} 에이전트 {stale_min}분째 미실행")
                    if agent_name in _SERVICE_MAP:
                        _action(f"{agent_name.upper()} 컨테이너 재시작 시도")
                        ok, info = restart_container(agent_name)
                        if ok:
                            _action(f"{agent_name.upper()} 재시작 성공: {info}")
                        else:
                            _alert(f"{agent_name.upper()} 재시작 실패: {info}")
            except (ValueError, TypeError):
                pass

    # --- 최종 보고 --------------------------------------------------------
    if report["alerts"] and not DRY_RUN:
        alert_lines = "\n".join(f"• {a}" for a in report["alerts"])
        action_lines = "\n".join(f"✅ {a}" for a in report["actions"]) if report["actions"] else "없음"
        _send_telegram(
            f"🔧 <b>[Self-Healer] 자동 진단 완료</b>\n"
            f"{_utc_now().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"<b>이슈:</b>\n{alert_lines}\n\n"
            f"<b>조치:</b>\n{action_lines}"
        )
    elif not report["alerts"]:
        _ok("모든 컴포넌트 정상")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaw 자가진단·수복 에이전트")
    parser.add_argument("--dry-run", action="store_true", help="실제 조치 없이 진단만 실행")
    parser.add_argument("--quiet", action="store_true", help="정상 항목 로그 생략")
    args = parser.parse_args()

    if args.dry_run:
        DRY_RUN = True

    result = run_self_healing(quiet=args.quiet)

    if result["alerts"]:
        print(f"⚠️  이슈 {len(result['alerts'])}건 | 조치 {len(result['actions'])}건")
        for a in result["alerts"]:
            print(f"  • {a}")
        sys.exit(1)
    else:
        print("✅ 정상")
        sys.exit(0)
