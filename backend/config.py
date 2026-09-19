from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
USER_CONFIG_DIR = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
) / "GPT-Web-Translator"
DEFAULT_ENV_FILE = USER_CONFIG_DIR / ".env"
LEGACY_ENV_FILE = PROJECT_DIR / ".env"
LEGACY_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _as_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None or not value.strip():
        return default
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"配置值必须在 {minimum} 到 {maximum} 之间")
    return number


def _as_float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"配置值必须在 {minimum} 到 {maximum} 之间")
    return number


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    host: str = "127.0.0.1"
    port: int = 8765
    timeout_seconds: int = 60
    max_concurrency: int = 2
    retries: int = 2
    max_batch_segments: int = 24
    max_batch_chars: int = 12000
    max_request_bytes: int = 512 * 1024
    max_output_tokens: int = 8000
    temperature: float = 0.2
    cache_max_entries: int = 20000
    prompt_version: str = "2026-08-v3-identity-mask"
    client_header: str = "gwt-extension-v1"
    data_dir: Path = PROJECT_DIR / "backend" / "data"

    @property
    def chat_completions_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/chat/completions"

    @property
    def balance_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/user/balance"

    def validate(self) -> None:
        parsed = urlparse(self.api_base)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("DEEPSEEK_API_BASE 必须是 HTTPS 地址")
        hostname = parsed.hostname.lower()
        if hostname != "deepseek.com" and not hostname.endswith(".deepseek.com"):
            raise ValueError("DEEPSEEK_API_BASE 必须指向 DeepSeek 官方域名")
        if self.host != "127.0.0.1":
            raise ValueError("服务只允许监听 127.0.0.1")
        if not self.model.strip():
            raise ValueError("DEEPSEEK_MODEL 不能为空")


def load_config(env_file: Path | None = None) -> Config:
    if env_file is not None:
        file_values = _parse_env_file(env_file)
    else:
        file_values = _parse_env_file(DEFAULT_ENV_FILE)
        if not file_values:
            file_values = _parse_env_file(LEGACY_ENV_FILE)

    def get(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, file_values.get(name, default))

    configured_model = (get("DEEPSEEK_MODEL", "deepseek-flash") or "").strip()
    config = Config(
        api_key=(get("DEEPSEEK_API_KEY", "") or "").strip(),
        api_base=(get("DEEPSEEK_API_BASE", "https://api.deepseek.com") or "").strip(),
        model=LEGACY_MODEL_ALIASES.get(configured_model.lower(), configured_model),
        port=_as_int(get("GWT_PORT"), 8765, 1024, 65535),
        timeout_seconds=_as_int(get("GWT_TIMEOUT_SECONDS"), 60, 10, 180),
        max_concurrency=_as_int(get("GWT_MAX_CONCURRENCY"), 2, 1, 8),
        retries=_as_int(get("GWT_RETRIES"), 2, 0, 4),
        max_batch_segments=_as_int(get("GWT_MAX_BATCH_SEGMENTS"), 24, 1, 64),
        max_batch_chars=_as_int(get("GWT_MAX_BATCH_CHARS"), 12000, 1000, 40000),
        max_output_tokens=_as_int(get("GWT_MAX_OUTPUT_TOKENS"), 8000, 512, 32000),
        temperature=_as_float(get("GWT_TEMPERATURE"), 0.2, 0.0, 1.0),
        cache_max_entries=_as_int(get("GWT_CACHE_MAX_ENTRIES"), 20000, 100, 200000),
    )
    config.validate()
    return config
