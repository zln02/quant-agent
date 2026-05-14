"""Environment variable loader - openclaw.json + .env files."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from common.config import OPENCLAW_JSON, OPENCLAW_ROOT, WORKSPACE

_loaded = False
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        hash_index = value.find(" #")
        if hash_index != -1:
            value = value[:hash_index].rstrip()
        parsed[key] = value.replace("\\n", "\n")
    return parsed


def load_env() -> None:
    """Load openclaw.json env + .env files into os.environ (idempotent)."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    if OPENCLAW_JSON.exists():
        try:
            data = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
            for k, v in (data.get("env") or {}).items():
                if k != "shellEnv" and isinstance(v, str):
                    os.environ.setdefault(k, v)
            telegram_token = ((data.get("channels") or {}).get("telegram") or {}).get("botToken")
            if isinstance(telegram_token, str) and telegram_token:
                os.environ.setdefault("TELEGRAM_BOT_TOKEN", telegram_token)
        except Exception:
            pass

    env_files = [
        Path(__file__).resolve().parents[1] / ".env",  # quant-agent/.env (host cron)
        OPENCLAW_ROOT / ".env",
        WORKSPACE / ".env",
        WORKSPACE / "skills" / "kiwoom-api" / ".env",
    ]
    for p in env_files:
        if not p.exists():
            continue
        try:
            for k, v in _parse_env_file(p).items():
                os.environ.setdefault(k, v)
        except Exception:
            continue

    # Docker/runtime secrets: container mount or local workspace secret directory.
    _quant_secrets = Path(__file__).resolve().parents[1] / ".docker-secrets"
    for secrets_dir in (Path("/run/local-secrets"), Path("/run/secrets/openclaw"), _quant_secrets, WORKSPACE / ".docker-secrets"):
        if not secrets_dir.is_dir():
            continue
        for secret_file in secrets_dir.iterdir():
            if secret_file.is_file() and _ENV_KEY_RE.match(secret_file.name):
                try:
                    value = secret_file.read_text(encoding="utf-8").strip()
                    if value:
                        os.environ.setdefault(secret_file.name, value)
                except Exception:
                    pass
