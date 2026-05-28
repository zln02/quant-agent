"""quant-agent → jay-agent best-effort bridge.

quant-agent의 모든 텔레그램 알림을 jay-agent (자비스 비서)의 FTS5 메모리에
동시에 인덱싱한다. 자비스가 "오늘 알림 뭐 있었어?" "BTC 매수했어?" 같은
질문에 진짜 데이터 기반으로 답할 수 있게 한다.

- Fire-and-forget: 실패해도 텔레그램 송신 흐름을 절대 막지 않음.
- Idempotent: 5초 윈도우 + 메시지 해시로 msg_id 생성 → 중복 송신 무시됨.
- 미설정 시 (JAY_AGENT_URL or JAY_INTERNAL_TOKEN 비어있음) silent skip.

Env keys:
    JAY_AGENT_URL          default http://127.0.0.1:8081
    JAY_INTERNAL_TOKEN     필수. jay-agent .env와 동일 값
    JAY_BRIDGE_TIMEOUT     default 2 (seconds)
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Optional

import requests

from common.env_loader import load_env
from common.logger import get_logger

load_env()
log = get_logger("jay_bridge")

_DEFAULT_URL = "http://127.0.0.1:8081"


def _config() -> tuple[str, str, float]:
    url = (os.environ.get("JAY_AGENT_URL") or _DEFAULT_URL).rstrip("/")
    token = os.environ.get("JAY_INTERNAL_TOKEN", "")
    try:
        timeout = float(os.environ.get("JAY_BRIDGE_TIMEOUT", "2"))
    except ValueError:
        timeout = 2.0
    return url, token, timeout


def _msg_id(message: str, priority: str, source: str, window_sec: int = 5) -> str:
    """5초 윈도우 해시 — 같은 메시지가 5초 안에 두 번 보내져도 같은 msg_id."""
    bucket = int(time.time() // window_sec)
    raw = f"{message}|{priority}|{source}|{bucket}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def post_event(
    message: str,
    priority: str = "URGENT",
    source: str = "telegram",
    snapshot: Optional[dict[str, Any]] = None,
) -> bool:
    """비차단 송신. True=요청 성공(중복 포함), False=실패/스킵.

    절대로 예외를 외부로 던지지 않는다. 호출자는 결과를 무시해도 안전.
    """
    url, token, timeout = _config()
    if not token:
        return False
    try:
        body: dict[str, Any] = {
            "msg_id": _msg_id(message, priority, source),
            "ts": time.time(),
            "priority": priority,
            "source": source,
            "message": message[:8000],
        }
        if snapshot:
            body["snapshot"] = snapshot
        r = requests.post(
            f"{url}/api/internal/quant-event",
            json=body,
            headers={"X-Internal-Token": token},
            timeout=timeout,
        )
        if r.status_code == 200:
            return True
        log.warning("jay_bridge upstream %s: %s", r.status_code, r.text[:200])
        return False
    except requests.exceptions.RequestException as exc:
        # 자비스가 안 떠 있어도 텔레그램 흐름은 살아남는다
        log.debug("jay_bridge unreachable: %s", exc)
        return False
    except Exception as exc:
        log.warning("jay_bridge unexpected: %s", exc)
        return False
