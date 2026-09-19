from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from cache import TranslationCache
from config import Config, load_config
from deepseek_client import DeepSeekClient, RequestCancelled, TranslationError
from token_usage import TokenUsageStore


LOGGER = logging.getLogger("gwt")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SEGMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, tuple[threading.Event, float]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> threading.Event:
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            current = self._sessions.get(session_id)
            if current is None:
                event = threading.Event()
                self._sessions[session_id] = (event, now)
                return event
            event, _ = current
            self._sessions[session_id] = (event, now)
            return event

    def cancel(self, session_id: str) -> bool:
        event = self.get(session_id)
        already_cancelled = event.is_set()
        event.set()
        return not already_cancelled

    def _cleanup_locked(self, now: float) -> None:
        stale = [key for key, (_, touched) in self._sessions.items() if now - touched > 3600]
        for key in stale:
            self._sessions.pop(key, None)


class ServerApp:
    def __init__(self, config: Config, client: DeepSeekClient | None = None):
        self.config = config
        self.cache = TranslationCache(config.data_dir / "translations.sqlite3", config.cache_max_entries)
        self.usage = TokenUsageStore(self.cache.database_path)
        self.client = client or DeepSeekClient(config, self.cache, self.usage)
        self.sessions = SessionRegistry()


class TranslationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler, app: ServerApp):
        super().__init__(address, handler)
        self.app = app


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "GPTWebTranslator/1.0"
    sys_version = ""

    @property
    def app(self) -> ServerApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("http client=%s " + format_string, self.client_address[0], *args)

    def _origin(self) -> str:
        return (self.headers.get("Origin") or "").strip()

    def _origin_allowed(self) -> bool:
        origin = self._origin().lower()
        return origin.startswith("chrome-extension://") or origin.startswith("extension://")

    def _client_allowed(self) -> bool:
        return self.headers.get("X-GWT-Client", "") == self.app.config.client_header

    def _set_cors(self) -> None:
        if self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", self._origin())
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: dict[str, Any], cors: bool = True) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self._set_cors()
        self.end_headers()
        self.wfile.write(encoded)

    def _forbidden(self) -> None:
        self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": {"code": "forbidden", "message": "请求来源不受信任"}}, cors=False)

    def _discard_request_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= self.app.config.max_request_bytes:
            self.rfile.read(length)

    def do_OPTIONS(self) -> None:
        requested_headers = {
            item.strip().lower()
            for item in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if item.strip()
        }
        if not self._origin_allowed() or "x-gwt-client" not in requested_headers:
            self._forbidden()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-GWT-Client")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}}, cors=False)
            return
        if self._origin() and not self._origin_allowed():
            self._forbidden()
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "GPT-Web-Translator",
                "configured": bool(self.app.config.api_key),
                "model": self.app.config.model,
                "token_stats_version": 3,
            },
            cors=self._origin_allowed(),
        )

    def do_POST(self) -> None:
        if not self._origin_allowed() or not self._client_allowed():
            self._discard_request_body()
            self._forbidden()
            return
        try:
            payload = self._read_json_body()
            if self.path == "/v1/translate":
                self._handle_translate(payload)
            elif self.path == "/v1/cancel":
                self._handle_cancel(payload)
            elif self.path in ("/v1/usage", "/v1/usage/clear"):
                page_id = validate_page_id(payload.get("page_id", ""))
                if self.path.endswith("/clear"):
                    if payload.get("confirmed") is not True:
                        raise TranslationError("confirmation_required", "请先确认清除历史统计", 400, False)
                    self.app.usage.clear_history()
                self._send_json(HTTPStatus.OK, {"ok": True, "token_stats": self.app.usage.snapshot(page_id)})
            elif self.path == "/v1/balance":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "account_balance": self.app.client.get_balance()},
                )
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}})
        except TranslationError as exc:
            self._send_json(exc.http_status, {"ok": False, "error": {"code": exc.code, "message": exc.user_message}})
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("client disconnected before response")
        except Exception:
            LOGGER.exception("unexpected request error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": {"code": "internal_error", "message": "本机翻译服务发生错误，请查看 backend/logs/service.log"}})

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise TranslationError("length_required", "请求内容长度缺失", 411, False)
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise TranslationError("bad_length", "请求内容长度无效", 400, False) from exc
        if length < 2 or length > self.app.config.max_request_bytes:
            raise TranslationError("request_too_large", "本次网页文本过多，请缩小批次后重试", 413, False)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationError("invalid_json", "本机服务收到的请求格式无效", 400, False) from exc
        if not isinstance(payload, dict):
            raise TranslationError("invalid_request", "本机服务收到的请求格式无效", 400, False)
        return payload

    def _handle_translate(self, payload: dict[str, Any]) -> None:
        session_id, clean_request = validate_translation_payload(payload, self.app.config)
        page_id = validate_page_id(payload.get("page_id", session_id))
        cancel_event = self.app.sessions.get(session_id)
        if cancel_event.is_set():
            raise RequestCancelled()
        LOGGER.info(
            "translate session=%s site=%s segments=%d chars=%d",
            session_id[:12],
            clean_request["page"]["site"][:80],
            len(clean_request["segments"]),
            sum(len(item["text"]) for item in clean_request["segments"]),
        )
        try:
            result = self.app.client.translate(clean_request, cancel_event, page_id)
        except TranslationError as exc:
            self._send_json(exc.http_status, {
                "ok": False, "error": {"code": exc.code, "message": exc.user_message},
                "token_stats": self.app.usage.snapshot(page_id),
            })
            return
        self._send_json(HTTPStatus.OK, {"ok": True, **result, "token_stats": self.app.usage.snapshot(page_id)})

    def _handle_cancel(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            raise TranslationError("invalid_session", "会话编号无效", 400, False)
        changed = self.app.sessions.cancel(session_id)
        self._send_json(HTTPStatus.OK, {"ok": True, "cancelled": changed})


def validate_page_id(value: Any) -> str:
    if not isinstance(value, str) or (value and not SESSION_RE.fullmatch(value)):
        raise TranslationError("invalid_page_id", "网页统计编号无效", 400, False)
    return value


def _clean_string(value: Any, max_length: int, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise TranslationError("invalid_request", "请求缺少必要文字字段", 400, False)
        return ""
    value = value.replace("\x00", "").strip()
    if required and not value:
        raise TranslationError("invalid_request", "请求缺少必要文字字段", 400, False)
    return value[:max_length]


def validate_translation_payload(payload: dict[str, Any], config: Config) -> tuple[str, dict[str, Any]]:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise TranslationError("invalid_session", "会话编号无效", 400, False)

    raw_page = payload.get("page")
    raw_segments = payload.get("segments")
    raw_context = payload.get("context", {})
    if not isinstance(raw_page, dict) or not isinstance(raw_segments, list) or not isinstance(raw_context, dict):
        raise TranslationError("invalid_request", "翻译请求格式不完整", 400, False)
    if not 1 <= len(raw_segments) <= config.max_batch_segments:
        raise TranslationError("invalid_segments", "翻译文本分段数量无效", 400, False)

    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    total_chars = 0
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise TranslationError("invalid_segment", "翻译文本分段格式无效", 400, False)
        segment_id = raw_segment.get("id")
        text = _clean_string(raw_segment.get("text"), 4000, required=True)
        if not isinstance(segment_id, str) or not SEGMENT_ID_RE.fullmatch(segment_id) or segment_id in seen_ids:
            raise TranslationError("invalid_segment_id", "翻译文本编号无效", 400, False)
        seen_ids.add(segment_id)
        total_chars += len(text)
        protected_terms: list[str] = []
        raw_protected = raw_segment.get("protected", [])
        if isinstance(raw_protected, list):
            for raw_term in raw_protected[:16]:
                clean_term = _clean_string(raw_term, 160)
                if clean_term and clean_term in text and clean_term not in protected_terms:
                    protected_terms.append(clean_term)
        clean_segment: dict[str, Any] = {"id": segment_id, "text": text}
        if protected_terms:
            clean_segment["protected"] = protected_terms
        segments.append(clean_segment)
    if total_chars > config.max_batch_chars:
        raise TranslationError("batch_too_large", "本次网页文本过多，请缩小批次后重试", 413, False)

    glossary: dict[str, str] = {}
    raw_glossary = raw_context.get("glossary", {})
    if isinstance(raw_glossary, dict):
        for key, value in list(raw_glossary.items())[:30]:
            if isinstance(key, str) and isinstance(value, str):
                clean_key = _clean_string(key, 80)
                clean_value = _clean_string(value, 80)
                if clean_key and clean_value:
                    glossary[clean_key] = clean_value

    clean_request = {
        "page": {
            "title": _clean_string(raw_page.get("title"), 300),
            "site": _clean_string(raw_page.get("site"), 200, required=True),
            "language": _clean_string(raw_page.get("language"), 40),
        },
        "context": {
            "previous_text": _clean_string(raw_context.get("previous_text"), 1600),
            "next_text": _clean_string(raw_context.get("next_text"), 1200),
            "glossary": glossary,
        },
        "segments": segments,
    }
    return session_id, clean_request


def configure_logging(data_dir: Path) -> None:
    log_dir = data_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "service.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


def create_server(app: ServerApp, port: int | None = None) -> TranslationHTTPServer:
    return TranslationHTTPServer((app.config.host, app.config.port if port is None else port), RequestHandler, app)


def write_pid_file(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    pid_file = data_dir / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    return pid_file


def main() -> int:
    try:
        config = load_config()
    except Exception as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    configure_logging(config.data_dir)
    app = ServerApp(config)
    pid_file = write_pid_file(config.data_dir)
    try:
        server = create_server(app)
    except OSError as exc:
        LOGGER.error("无法启动本机服务：%s", exc)
        pid_file.unlink(missing_ok=True)
        return 3

    def stop_server(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_server)
    LOGGER.info(
        "GPT-Web-Translator started at http://%s:%d (configured=%s model=%s)",
        config.host,
        config.port,
        bool(config.api_key),
        config.model,
    )
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        LOGGER.info("收到停止命令")
    finally:
        server.server_close()
        pid_file.unlink(missing_ok=True)
        LOGGER.info("GPT-Web-Translator stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
