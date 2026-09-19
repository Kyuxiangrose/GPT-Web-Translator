from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from cache import TranslationCache
from config import Config
from token_usage import TokenUsageStore, clean_usage


LOGGER = logging.getLogger("gwt.deepseek")


@dataclass
class TranslationError(Exception):
    code: str
    user_message: str
    http_status: int = 502
    retryable: bool = False

    def __str__(self) -> str:
        return self.user_message


class RequestCancelled(TranslationError):
    def __init__(self):
        super().__init__("cancelled", "翻译已停止", 409, False)


SYSTEM_PROMPT = """你是严谨的网页翻译引擎。把输入 JSON 中 segments 的自然语言文本翻译为自然、准确的简体中文。

必须遵守：
1. 先结合页面主题、站点语境、相邻文本、previous_text 和 glossary 理解语义，再翻译；语义正确优先于逐词对应。
2. 保持作者语气、逻辑与信息完整，不总结、不删减、不添加原文没有的信息。
3. 用户名、账号名、显示名、昵称、频道名、创作者名及 @handle 属于身份标识，必须逐字符保持原文（包括大小写、空格、符号、Emoji 和认证标记）；绝对不得翻译、音译、意译、纠错或改写。形如 __GWT_IDENTITY_0__ 的身份占位符必须逐字符保留在原位置，绝对不得删除或改变。
4. 人名、品牌、产品、软件、游戏、缩写、专业或社区术语可保留原文；必要时使用“中文译名（English Name）”，但不要滥用。
5. 代码、变量名、文件路径、URL、命令和技术标识符原则上保持不变。
6. glossary 中语义未改变的术语应保持一致；只有上下文明显改变时才重新判断。
7. 每个输入 id 必须原样出现在输出中且只出现一次，不得合并、拆分或遗漏。
8. 只输出 JSON 对象，不得输出 Markdown、解释或“以下是翻译”等额外文字。

输出 JSON 格式示例：
{"translations":[{"id":"s1","text":"译文"}],"glossary":{"bubble":"圈子"}}
glossary 只保留对本页后续翻译确有帮助的专名或多义术语，最多 30 项。"""


HANDLE_RE = re.compile(r"@[\w.-]{1,64}", re.UNICODE)
IDENTITY_PREFIX_PATTERNS = (
    re.compile(r"^(?P<name>[^\n:：]{1,80}?)\s*\(\s*(?P<handle>@[\w.-]{1,64})\s*\)\s*(?=[:：·|—–-])", re.UNICODE),
    re.compile(r"^(?P<name>[^\n:：@]{1,80}?)\s+(?P<handle>@[\w.-]{1,64})\s*(?=[·|:：—–-])", re.UNICODE),
)


def _identity_terms(segment: dict[str, Any]) -> list[str]:
    text = str(segment.get("text") or "")
    terms: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        clean = value.strip()
        if clean and len(clean) <= 160 and clean in text and clean not in terms:
            terms.append(clean)

    protected = segment.get("protected", [])
    if isinstance(protected, list):
        for term in protected[:16]:
            add(term)
    for match in HANDLE_RE.finditer(text):
        add(match.group(0))
    for pattern in IDENTITY_PREFIX_PATTERNS:
        match = pattern.search(text)
        if match:
            add(match.group("name"))
            add(match.group("handle"))
    return sorted(terms, key=len, reverse=True)


def _mask_identity_terms(request_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
    masked = json.loads(json.dumps(request_payload, ensure_ascii=False))
    all_text = json.dumps(masked, ensure_ascii=False)
    term_to_token: dict[str, str] = {}
    expected_by_id: dict[str, list[str]] = {}

    for segment in request_payload.get("segments", []):
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("id") or "")
        expected_by_id.setdefault(segment_id, [])
        for term in _identity_terms(segment):
            token = term_to_token.get(term)
            if token is None:
                token = f"__GWT_IDENTITY_{len(term_to_token)}__"
                while token in all_text:
                    token = f"_{token}_"
                term_to_token[term] = token
                all_text += token
            expected_by_id[segment_id].append(token)

    token_to_term = {token: term for term, token in term_to_token.items()}

    def replace(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        result = value
        for term, token in sorted(term_to_token.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(term, token)
        return result

    page = masked.get("page", {})
    if isinstance(page, dict):
        for key in ("title", "site", "language"):
            page[key] = replace(page.get(key))
    context = masked.get("context", {})
    if isinstance(context, dict):
        for key in ("previous_text", "next_text"):
            context[key] = replace(context.get(key))
        glossary = context.get("glossary", {})
        if isinstance(glossary, dict):
            context["glossary"] = {replace(key): replace(value) for key, value in glossary.items()}
    for segment in masked.get("segments", []):
        if isinstance(segment, dict):
            segment["text"] = replace(segment.get("text"))
            segment.pop("protected", None)
    return masked, token_to_term, expected_by_id


def _restore_identity_terms(
    validated: dict[str, Any],
    token_to_term: dict[str, str],
    expected_by_id: dict[str, list[str]],
) -> dict[str, Any]:
    for item in validated.get("translations", []):
        segment_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        for token in expected_by_id.get(segment_id, []):
            if token not in text:
                raise TranslationError("identity_placeholder_missing", "DeepSeek 未能保持账号名称，请重试", 502, True)
        for token, term in token_to_term.items():
            text = text.replace(token, term)
        item["text"] = text
    glossary = validated.get("glossary", {})
    if isinstance(glossary, dict):
        restored: dict[str, str] = {}
        for key, value in glossary.items():
            clean_key = key
            clean_value = value
            for token, term in token_to_term.items():
                clean_key = clean_key.replace(token, term)
                clean_value = clean_value.replace(token, term)
            restored[clean_key] = clean_value
        validated["glossary"] = restored
    return validated


class DeepSeekClient:
    def __init__(self, config: Config, cache: TranslationCache, usage_store: TokenUsageStore | None = None):
        self.config = config
        self.cache = cache
        self.usage_store = usage_store or TokenUsageStore(cache.database_path)
        self._semaphore = threading.BoundedSemaphore(config.max_concurrency)
        self._balance_lock = threading.Lock()
        self._balance_cache: dict[str, Any] | None = None
        self._balance_cached_at = 0.0

    def get_balance(self) -> dict[str, Any]:
        """Return the official account balance without exposing the API key."""
        if not self.config.api_key:
            raise TranslationError(
                "not_configured",
                "尚未配置 DeepSeek API Key，请先运行“配置API密钥.bat”",
                503,
                False,
            )
        with self._balance_lock:
            now = time.monotonic()
            if self._balance_cache is not None and now - self._balance_cached_at < 60:
                return dict(self._balance_cache)
            request = urllib.request.Request(
                self.config.balance_url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Accept": "application/json",
                    "User-Agent": "GPT-Web-Translator/1.0",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read(256 * 1024)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TranslationError(
                        "invalid_key", "DeepSeek API Key 无效", 502, False
                    ) from exc
                if exc.code == 429:
                    raise TranslationError(
                        "rate_limited", "DeepSeek 当前请求较多，请稍后重试", 503, True
                    ) from exc
                raise TranslationError(
                    "balance_unavailable", "暂时无法读取 DeepSeek 账户余额", 502, False
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise TranslationError(
                    "balance_unavailable", "暂时无法读取 DeepSeek 账户余额", 503, True
                ) from exc
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TranslationError(
                    "invalid_balance_response", "DeepSeek 返回了无法识别的余额信息", 502, False
                ) from exc
            if not isinstance(envelope, dict) or not isinstance(
                envelope.get("balance_infos"), list
            ):
                raise TranslationError(
                    "invalid_balance_response", "DeepSeek 返回了无法识别的余额信息", 502, False
                )
            balances = []
            for item in envelope["balance_infos"]:
                if not isinstance(item, dict) or item.get("currency") not in {"CNY", "USD"}:
                    continue
                cleaned = {"currency": item["currency"]}
                valid = True
                for key in ("total_balance", "granted_balance", "topped_up_balance"):
                    try:
                        amount = Decimal(str(item.get(key, "")))
                    except (InvalidOperation, ValueError):
                        valid = False
                        break
                    if not amount.is_finite() or amount < 0:
                        valid = False
                        break
                    cleaned[key] = format(amount, "f")
                if valid:
                    balances.append(cleaned)
            if not balances:
                raise TranslationError(
                    "invalid_balance_response", "DeepSeek 返回了无法识别的余额信息", 502, False
                )
            result = {
                "is_available": envelope.get("is_available") is True,
                "balance_infos": balances,
                "fetched_at": int(time.time()),
            }
            self._balance_cache = result
            self._balance_cached_at = now
            return dict(result)

    def _cache_key(self, request_payload: dict[str, Any]) -> str:
        material = {
            "prompt_version": self.config.prompt_version,
            "model": self.config.model,
            "request": request_payload,
        }
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def translate(
        self,
        request_payload: dict[str, Any],
        cancel_event: threading.Event,
        page_id: str = "",
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise RequestCancelled()

        cache_key = self._cache_key(request_payload)
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            # Old caches contain usage from the original API call. Reuse costs zero.
            cached["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            return cached

        if not self.config.api_key:
            raise TranslationError(
                "not_configured",
                "尚未配置 DeepSeek API Key，请先运行“配置API密钥.bat”",
                503,
                False,
            )

        acquired = False
        while not acquired:
            if cancel_event.is_set():
                raise RequestCancelled()
            acquired = self._semaphore.acquire(timeout=0.2)

        try:
            last_error: TranslationError | None = None
            for attempt in range(self.config.retries + 1):
                if cancel_event.is_set():
                    raise RequestCancelled()
                try:
                    result = self._call_api(request_payload, cancel_event, page_id)
                    if cancel_event.is_set():
                        raise RequestCancelled()
                    cache_value = dict(result)
                    cache_value.pop("usage", None)
                    cache_value["cached"] = False
                    self.cache.put(cache_key, cache_value)
                    return {**result, "cached": False}
                except TranslationError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= self.config.retries:
                        raise
                    delay = min(0.8 * (2**attempt), 4.0)
                    LOGGER.warning("DeepSeek request retry: code=%s attempt=%d", exc.code, attempt + 1)
                    if cancel_event.wait(delay):
                        raise RequestCancelled()
            assert last_error is not None
            raise last_error
        finally:
            self._semaphore.release()

    def _call_api(
        self,
        request_payload: dict[str, Any],
        cancel_event: threading.Event,
        page_id: str = "",
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise RequestCancelled()

        protected_payload, token_to_term, expected_by_id = _mask_identity_terms(request_payload)
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请按要求翻译以下 JSON，并只返回 JSON：\n"
                    + json.dumps(protected_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "GPT-Web-Translator/1.0",
            },
        )

        event_id = uuid.uuid4().hex
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(8192).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            LOGGER.warning("DeepSeek HTTP error: status=%s detail_length=%d", exc.code, len(detail))
            if exc.code in (401, 403):
                raise TranslationError("invalid_key", "DeepSeek API Key 无效或无权使用该模型", 502, False)
            if exc.code in (402,):
                raise TranslationError("insufficient_balance", "DeepSeek 账户余额不足", 502, False)
            if exc.code == 429:
                raise TranslationError("rate_limited", "DeepSeek 当前请求较多，请稍后重试", 503, True)
            if 500 <= exc.code <= 599:
                raise TranslationError("upstream_unavailable", "DeepSeek 服务暂时不可用", 503, True)
            raise TranslationError("upstream_rejected", "DeepSeek 拒绝了本次翻译请求", 502, False)
        except urllib.error.URLError as exc:
            reason_name = type(getattr(exc, "reason", exc)).__name__
            LOGGER.warning("DeepSeek network error: %s", reason_name)
            raise TranslationError("network_error", "无法连接 DeepSeek，请检查网络后重试", 503, True)
        except TimeoutError:
            raise TranslationError("timeout", "DeepSeek 响应超时，请稍后重试", 504, True)

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationError("invalid_response", "DeepSeek 返回了无法识别的结果", 502, True) from exc

        # Account before content validation/cancellation/retry: a successful API
        # response may already have consumed tokens even when its text is unusable.
        if isinstance(envelope, dict) and not envelope.get("error"):
            try:
                actual_model = envelope.get("model") or self.config.model
                self.usage_store.record(
                    event_id, page_id, envelope.get("usage"),
                    actual_model,
                    envelope.get("created"),
                )
            except sqlite3.Error as exc:
                # Never retry a paid API call because writing its receipt failed.
                raise TranslationError("usage_storage_error", "Token 统计无法写入本机数据库，请检查磁盘后重试", 500, False) from exc
        try:
            message = envelope["choices"][0]["message"]
            content = message["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("invalid_response", "DeepSeek 返回了无法识别的结果", 502, True) from exc

        if not isinstance(content, str) or not content.strip():
            raise TranslationError("empty_response", "DeepSeek 返回了空结果，请重试", 502, True)
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise TranslationError("invalid_json", "DeepSeek 返回格式异常，请重试", 502, True) from exc

        validated = self._validate_model_output(parsed, protected_payload)
        validated = _restore_identity_terms(validated, token_to_term, expected_by_id)
        validated["usage"] = clean_usage(envelope.get("usage"))
        validated["model"] = envelope.get("model") or self.config.model
        return validated

    @staticmethod
    def _validate_model_output(parsed: Any, request_payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("translations"), list):
            raise TranslationError("invalid_shape", "DeepSeek 返回格式不完整，请重试", 502, True)

        expected_ids = [segment["id"] for segment in request_payload["segments"]]
        by_id: dict[str, str] = {}
        for item in parsed["translations"]:
            if not isinstance(item, dict):
                continue
            segment_id = item.get("id")
            text = item.get("text")
            if segment_id in expected_ids and isinstance(text, str) and text.strip() and segment_id not in by_id:
                by_id[segment_id] = text.strip()
        if set(by_id) != set(expected_ids):
            raise TranslationError("missing_segments", "DeepSeek 返回的译文有缺失，请重试", 502, True)

        glossary_raw = parsed.get("glossary", {})
        glossary: dict[str, str] = {}
        if isinstance(glossary_raw, dict):
            for key, value in list(glossary_raw.items())[:30]:
                if isinstance(key, str) and isinstance(value, str):
                    key = key.strip()[:80]
                    value = value.strip()[:80]
                    if key and value:
                        glossary[key] = value
        return {
            "translations": [{"id": segment_id, "text": by_id[segment_id]} for segment_id in expected_ids],
            "glossary": glossary,
        }
