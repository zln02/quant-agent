"""PR #25 _call_btc_haiku 정책 단위 테스트.

7 케이스: success / 401 / 429 / timeout-retry-success / timeout-retry-fail / parse / empty.
patch.object 로 client 주입. cooldown 파일은 tmp_path fixture 로 격리.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402  (테스트 환경에서 anthropic 설치 가정)


@pytest.fixture(autouse=True)
def _isolate_cooldown(tmp_path, monkeypatch):
    """각 테스트마다 cooldown 파일 격리 + 모듈 캐시 클라이언트 리셋."""
    from btc import btc_trading_agent as mod
    monkeypatch.setattr(mod, "_BTC_HAIKU_COOLDOWN_FILE", tmp_path / "btc_haiku_cooldown.ts")
    monkeypatch.setattr(mod, "_BTC_HAIKU_CLIENT", None)
    yield


def _mk_msg(text, in_tok=120, out_tok=60):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)] if text else []
    usage = MagicMock()
    usage.input_tokens = in_tok
    usage.output_tokens = out_tok
    msg.usage = usage
    return msg


def _api_err(cls, message):
    """anthropic API exception 인스턴스 생성 (SDK 버전 호환 fallback)."""
    try:
        return cls(message=message, response=MagicMock(status_code=500), body=None)
    except TypeError:
        try:
            return cls(message)
        except TypeError:
            return cls()


def test_claude_success_returns_parsed_with_meta():
    from btc import btc_trading_agent as mod
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mk_msg(
        '{"action": "BUY", "confidence": 75, "reason": "test"}'
    )
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("user", "system")
    assert parsed is not None and parsed["action"] == "BUY"
    assert meta["decision_source"] == "AI"
    assert meta["model"] == "claude-haiku-4-5-20251001"
    assert meta["ai_latency_ms"] is not None and meta["ai_latency_ms"] >= 0
    assert meta["prompt_tokens"] == 120
    assert meta["response_tokens"] == 60
    assert not mod._BTC_HAIKU_COOLDOWN_FILE.exists()


def test_401_returns_none_sets_cooldown():
    from btc import btc_trading_agent as mod
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _api_err(
        anthropic.AuthenticationError, "invalid_api_key"
    )
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is None
    assert mod._BTC_HAIKU_COOLDOWN_FILE.exists()
    assert mod._BTC_HAIKU_COOLDOWN_FILE.read_text() == "AUTH_401"


def test_429_returns_none_sets_cooldown():
    from btc import btc_trading_agent as mod
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _api_err(
        anthropic.RateLimitError, "rate_limit"
    )
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is None
    assert mod._BTC_HAIKU_COOLDOWN_FILE.read_text() == "RATE_429"


def test_timeout_retry_then_success():
    from btc import btc_trading_agent as mod
    timeout_err = _api_err(anthropic.APITimeoutError, "timeout")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        timeout_err,
        _mk_msg('{"action": "HOLD", "confidence": 50, "reason": "after retry"}'),
    ]
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is not None and parsed["action"] == "HOLD"
    assert not mod._BTC_HAIKU_COOLDOWN_FILE.exists()


def test_timeout_retry_then_fail_returns_none():
    from btc import btc_trading_agent as mod
    timeout_err = _api_err(anthropic.APITimeoutError, "timeout")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [timeout_err, timeout_err]
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is None
    assert not mod._BTC_HAIKU_COOLDOWN_FILE.exists()  # timeout은 cooldown 미설정


def test_parse_error_returns_none_no_cooldown():
    from btc import btc_trading_agent as mod
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mk_msg("not valid json {")
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is None
    assert not mod._BTC_HAIKU_COOLDOWN_FILE.exists()  # parse는 transient


def test_empty_response_returns_none_sets_cooldown():
    from btc import btc_trading_agent as mod
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mk_msg("")  # 빈 content
    with patch.object(mod, "_btc_haiku_get_client", return_value=mock_client):
        parsed, meta = mod._call_btc_haiku("p", "s")
    assert parsed is None
    assert mod._BTC_HAIKU_COOLDOWN_FILE.read_text() == "EMPTY"
