"""자비스(jay-agent) bridge 단위 테스트.

- post_event: 토큰 미설정 시 skip, 정상 200 시 True, 5xx 시 False.
- send_telegram: 자비스 성공 시 텔레그램 skip, 자비스 실패 시 텔레그램 fallback.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from common import jay_bridge
from common.telegram import Priority, send_telegram


def test_post_event_skip_without_token(monkeypatch):
    monkeypatch.delenv("JAY_INTERNAL_TOKEN", raising=False)
    assert jay_bridge.post_event("msg") is False


def test_post_event_success(monkeypatch):
    monkeypatch.setenv("JAY_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("JAY_AGENT_URL", "http://127.0.0.1:8081")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("common.jay_bridge.requests.post", return_value=mock_resp) as mock_post:
        assert jay_bridge.post_event("hello", priority="URGENT") is True
        mock_post.assert_called_once()
        body = mock_post.call_args.kwargs["json"]
        assert body["message"] == "hello"
        assert body["priority"] == "URGENT"
        assert body["source"] == "telegram"
        assert "msg_id" in body and len(body["msg_id"]) == 24


def test_post_event_upstream_5xx(monkeypatch):
    monkeypatch.setenv("JAY_INTERNAL_TOKEN", "test-token")
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "internal error"
    with patch("common.jay_bridge.requests.post", return_value=mock_resp):
        assert jay_bridge.post_event("msg") is False


def test_msg_id_idempotency_within_window():
    a = jay_bridge._msg_id("msg", "URGENT", "quant", window_sec=5)
    b = jay_bridge._msg_id("msg", "URGENT", "quant", window_sec=5)
    assert a == b


def test_send_telegram_jay_success_skips_telegram(monkeypatch):
    """자비스 발송 성공 시 텔레그램 호출 안 됨."""
    monkeypatch.setenv("JAY_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "tg-chat")
    with patch("common.jay_bridge.post_event", return_value=True) as mock_jay, \
         patch("common.telegram.requests.post") as mock_tg:
        assert send_telegram("test", priority=Priority.URGENT) is True
        mock_jay.assert_called_once()
        mock_tg.assert_not_called()


def test_send_telegram_jay_fail_falls_back(monkeypatch):
    """자비스 실패 시 텔레그램 fallback."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "tg-chat")
    mock_tg_resp = MagicMock()
    mock_tg_resp.ok = True
    mock_tg_resp.status_code = 200
    with patch("common.jay_bridge.post_event", return_value=False), \
         patch("common.telegram.requests.post", return_value=mock_tg_resp) as mock_tg:
        assert send_telegram("test", priority=Priority.URGENT) is True
        mock_tg.assert_called_once()


def test_send_telegram_info_buffered_not_routed(monkeypatch):
    """INFO 등급은 자비스도 텔레그램도 호출 안 됨, 버퍼만 사용."""
    with patch("common.jay_bridge.post_event") as mock_jay, \
         patch("common.telegram.requests.post") as mock_tg, \
         patch("common.telegram.append_info_buffer") as mock_buf:
        assert send_telegram("info msg", priority=Priority.INFO) is True
        mock_jay.assert_not_called()
        mock_tg.assert_not_called()
        mock_buf.assert_called_once_with("info msg")
