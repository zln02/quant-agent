"""Structured logging for all OpenClaw agents.

Usage:
    from common.logger import get_logger
    log = get_logger("btc_agent")
    log.info("매매 사이클 시작")
    log.trade("BTC 매수", price=142000000, qty=0.001)
    log.warning("거래량 급감")
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from common.config import LOG_DIR

# Ensure log directory exists
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 로그 파일·디렉터리 권한: 소유자 RW, 그룹 R, 기타 차단 (640/750)
# 2026-04-19 보안감사 H-1: 기존 644가 world-readable이라 Supabase JWT·Kiwoom 토큰 노출 위험.
_LOG_FILE_MODE = 0o640
_LOG_DIR_MODE = 0o750


def _chmod_safe(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, PermissionError):
        pass


_chmod_safe(LOG_DIR, _LOG_DIR_MODE)

_FMT = "[%(asctime)s][%(name)s][%(levelname)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Custom TRADE level (between INFO and WARNING)
TRADE_LEVEL = 25
logging.addLevelName(TRADE_LEVEL, "TRADE")

_loggers: dict[str, "AgentLogger"] = {}

_JSON_RESERVED = {
    "name", "msg", "args", "levelname", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created",
    "msecs", "relativeCreated", "thread", "threadName", "processName",
    "process", "message", "asctime",
}


def _json_safe(value):
    """Convert logging extras into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


_SENSITIVE_RE = re.compile(
    r"(sk-proj-|sk-ant-api|Bearer |eyJhbG)[A-Za-z0-9_/+=\-]{8,}",
)


def _redact(msg: str) -> str:
    """민감 정보(API 키 등) 마스킹."""
    return _SENSITIVE_RE.sub(lambda m: m.group()[:8] + "***REDACTED***", msg)


class JsonFormatter(logging.Formatter):
    """로그 레코드를 JSON-line 으로 직렬화 (구조화 로그용)."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": _redact(record.message),
        }
        # log.trade("BTC 매수", market="btc", action="buy", price=142000000)
        # → extra 필드로 JSON에 포함
        for key, val in record.__dict__.items():
            if key not in _JSON_RESERVED and not key.startswith("_"):
                payload[key] = _json_safe(val)
        return json.dumps(payload, ensure_ascii=False)


class AgentLogger:
    """Thin wrapper around stdlib logging with a TRADE level and emoji prefixes."""

    EMOJI = {
        "DEBUG": "🔍",
        "INFO": "ℹ️",
        "TRADE": "💰",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "CRITICAL": "🚨",
    }

    def __init__(self, name: str, log_file: Optional[Path] = None):
        self._log = logging.getLogger(f"openclaw.{name}")
        if self._log.handlers:
            return  # already configured
        self._log.setLevel(logging.DEBUG)

        formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

        # Console handler (INFO+)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(formatter)
        self._log.addHandler(ch)

        # File handler — 항상 활성화 (Docker 컨테이너 포함)
        if log_file is None:
            log_file = LOG_DIR / f"{name}.log"
        if True:
            try:
                if log_file.exists() and not os.access(log_file, os.W_OK):
                    try:
                        log_file.unlink()
                    except PermissionError:
                        pass
                fh = RotatingFileHandler(
                    log_file,
                    maxBytes=10 * 1024 * 1024,  # 10MB
                    backupCount=5,
                    encoding="utf-8",
                )
                fh.setLevel(logging.DEBUG)
                fh.setFormatter(formatter)
                self._log.addHandler(fh)
                _chmod_safe(log_file, _LOG_FILE_MODE)
            except PermissionError:
                pass

        # JSON handler — 구조화 로그 (DEBUG+)
        json_dir = LOG_DIR / "json"
        json_dir.mkdir(parents=True, exist_ok=True)
        _chmod_safe(json_dir, _LOG_DIR_MODE)
        jsonl_path = json_dir / f"{name}.jsonl"
        try:
            # 권한 문제(root가 먼저 생성한 경우)를 방어: 쓰기 가능하면 그대로, 아니면 삭제 후 재생성
            if jsonl_path.exists() and not os.access(jsonl_path, os.W_OK):
                try:
                    jsonl_path.unlink()
                except PermissionError:
                    pass  # 삭제도 안 되면 JSON 핸들러 없이 진행
            jfh = RotatingFileHandler(
                jsonl_path,
                maxBytes=50 * 1024 * 1024,  # 50MB
                backupCount=3,
                encoding="utf-8",
            )
            jfh.setLevel(logging.DEBUG)
            jfh.setFormatter(JsonFormatter())
            self._log.addHandler(jfh)
            _chmod_safe(jsonl_path, _LOG_FILE_MODE)
        except PermissionError:
            pass  # JSON 구조화 로그 생략, 텍스트 로그는 정상 작동

    # ── convenience methods ──

    def debug(self, msg: str, *args, **kw):
        self._log.debug(self._fmt(msg % args if args else msg, "DEBUG", **kw), extra=kw)

    def info(self, msg: str, *args, **kw):
        self._log.info(self._fmt(msg % args if args else msg, "INFO", **kw), extra=kw)

    def trade(self, msg: str, *args, **kw):
        self._log.log(TRADE_LEVEL, self._fmt(msg % args if args else msg, "TRADE", **kw), extra=kw)

    def warn(self, msg: str, *args, **kw):
        self._log.warning(self._fmt(msg % args if args else msg, "WARNING", **kw), extra=kw)

    # Python logging 표준 메서드명 호환
    warning = warn

    def error(self, msg: str, *args, **kw):
        exc = kw.pop("exc_info", False)
        self._log.error(self._fmt(msg % args if args else msg, "ERROR", **kw), extra=kw, exc_info=exc)

    def critical(self, msg: str, *args, **kw):
        exc = kw.pop("exc_info", False)
        self._log.critical(self._fmt(msg % args if args else msg, "CRITICAL", **kw), extra=kw, exc_info=exc)

    @classmethod
    def _fmt(cls, msg: str, level: str, **kw) -> str:
        """텍스트 로그 포맷 (기존 형식 유지)."""
        prefix = cls.EMOJI.get(level, "")
        extra = ""
        if kw:
            parts = [f"{k}={v}" for k, v in kw.items()]
            extra = " | " + ", ".join(parts)
        return _redact(f"{prefix} {msg}{extra}")


def get_logger(name: str, log_file: Optional[Path] = None) -> AgentLogger:
    """Get or create a named logger (singleton per name)."""
    if name not in _loggers:
        _loggers[name] = AgentLogger(name, log_file)
    return _loggers[name]
