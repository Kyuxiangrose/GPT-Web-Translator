from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from cache import TranslationCache  # noqa: E402
from config import Config  # noqa: E402
from deepseek_client import DeepSeekClient, RequestCancelled, SYSTEM_PROMPT, TranslationError  # noqa: E402
from server import ServerApp, create_server, validate_translation_payload  # noqa: E402


def sample_payload() -> dict:
    return {
        "session_id": "gwt_1234567890",
        "page": {"title": "Example", "site": "example.com", "language": "en"},
        "context": {"previous_text": "", "next_text": "", "glossary": {}},
        "segments": [
            {"id": "s1", "text": "Hello world"},
            {"id": "s2", "text": "Learn more about DeepSeek"},
        ],
    }


class StubDeepSeekClient(DeepSeekClient):
    def __init__(self, config, cache):
        super().__init__(config, cache)
        self.calls = 0
        self.failures_remaining = 0

    def _call_api(self, request_payload, cancel_event, page_id=""):
        self.calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise TranslationError("temporary", "temporary", 503, True)
        return {
            "translations": [
                {"id": item["id"], "text": f"译文：{item['text']}"}
                for item in request_payload["segments"]
            ],
            "glossary": {"DeepSeek": "DeepSeek"},
            "model": self.config.model,
        }


class BackendUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.config = Config(
            api_key="unit-test",
            data_dir=self.temp_path / "data",
            retries=0,
            max_concurrency=2,
        )
        self.cache = TranslationCache(self.temp_path / "cache.sqlite3", max_entries=10)

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_round_trip_and_hit_count(self):
        self.cache.put("abc", {"translations": [{"id": "s1", "text": "你好"}]})
        value = self.cache.get("abc")
        self.assertEqual(value["translations"][0]["text"], "你好")
        self.assertEqual(self.cache.count(), 1)

    def test_prompt_strictly_preserves_account_identity_text(self):
        for protected_term in ("用户名", "显示名", "昵称", "频道名", "@handle", "逐字符保持原文", "绝对不得翻译"):
            self.assertIn(protected_term, SYSTEM_PROMPT)
        self.assertEqual(self.config.prompt_version, "2026-08-v3-identity-mask")

    def test_api_masks_and_restores_inline_identity_terms(self):
        client = DeepSeekClient(self.config, self.cache)
        payload = sample_payload()
        payload["segments"] = [{
            "id": "s1",
            "text": "The Western Journal (@WestJournalism): One year later, D.C. is shining again.",
            "protected": ["The Western Journal", "@WestJournalism"],
        }]
        _, clean = validate_translation_payload(payload, self.config)
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.envelope, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            model_input = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
            masked_text = model_input["segments"][0]["text"]
            captured["masked_text"] = masked_text
            response = FakeResponse()
            response.envelope = {
                "choices": [{"message": {"content": json.dumps({
                    "translations": [{"id": "s1", "text": f"译文：{masked_text}"}],
                    "glossary": {},
                }, ensure_ascii=False)}}],
                "usage": {},
            }
            return response

        with patch("deepseek_client.urllib.request.urlopen", fake_urlopen):
            result = client._call_api(clean, threading.Event())

        self.assertNotIn("The Western Journal", captured["masked_text"])
        self.assertNotIn("@WestJournalism", captured["masked_text"])
        self.assertIn("__GWT_IDENTITY_", captured["masked_text"])
        translated = result["translations"][0]["text"]
        self.assertIn("The Western Journal", translated)
        self.assertIn("@WestJournalism", translated)
        self.assertNotIn("__GWT_IDENTITY_", translated)

    def test_client_reuses_context_cache(self):
        client = StubDeepSeekClient(self.config, self.cache)
        _, clean = validate_translation_payload(sample_payload(), self.config)
        first = client.translate(clean, threading.Event())
        second = client.translate(clean, threading.Event())
        self.assertEqual(client.calls, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    def test_retry_then_success(self):
        config = Config(
            api_key="unit-test",
            data_dir=self.temp_path / "retry-data",
            retries=1,
            max_concurrency=1,
        )
        cache = TranslationCache(self.temp_path / "retry-cache.sqlite3")
        client = StubDeepSeekClient(config, cache)
        client.failures_remaining = 1
        _, clean = validate_translation_payload(sample_payload(), config)
        result = client.translate(clean, threading.Event())
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(result["translations"]), 2)

    def test_cancelled_request_never_calls_api(self):
        client = StubDeepSeekClient(self.config, self.cache)
        _, clean = validate_translation_payload(sample_payload(), self.config)
        event = threading.Event()
        event.set()
        with self.assertRaises(RequestCancelled):
            client.translate(clean, event)
        self.assertEqual(client.calls, 0)

    def test_validation_rejects_duplicate_segment_ids(self):
        payload = sample_payload()
        payload["segments"][1]["id"] = "s1"
        with self.assertRaises(TranslationError) as context:
            validate_translation_payload(payload, self.config)
        self.assertEqual(context.exception.code, "invalid_segment_id")

    def test_validation_truncates_context_and_never_accepts_url_field(self):
        payload = sample_payload()
        payload["page"]["url"] = "https://example.com/private?token=secret"
        _, clean = validate_translation_payload(payload, self.config)
        self.assertNotIn("url", clean["page"])

    def test_model_output_requires_every_segment_once(self):
        _, clean = validate_translation_payload(sample_payload(), self.config)
        with self.assertRaises(TranslationError) as context:
            DeepSeekClient._validate_model_output(
                {"translations": [{"id": "s1", "text": "你好"}]},
                clean,
            )
        self.assertEqual(context.exception.code, "missing_segments")

    def test_official_chat_completions_request_shape(self):
        client = DeepSeekClient(self.config, self.cache)
        _, clean = validate_translation_payload(sample_payload(), self.config)
        translated = {
            "translations": [
                {"id": "s1", "text": "你好，世界"},
                {"id": "s2", "text": "了解更多 DeepSeek 信息"},
            ],
            "glossary": {"DeepSeek": "DeepSeek"},
        }
        envelope = {
            "choices": [{"message": {"content": json.dumps(translated, ensure_ascii=False)}}],
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
            },
            "model": "deepseek-v4-flash",
            "created": 1789171200,
        }
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.get_header("Authorization")
            return FakeResponse()

        with patch("deepseek_client.urllib.request.urlopen", fake_urlopen):
            result = client._call_api(clean, threading.Event())

        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["body"]["model"], "deepseek-flash")
        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["authorization"], "Bearer unit-test")
        self.assertEqual(result["translations"][1]["text"], "了解更多 DeepSeek 信息")

    def test_official_balance_is_validated_and_cached(self):
        client = DeepSeekClient(self.config, self.cache)
        captured = {"calls": 0}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({
                    "is_available": True,
                    "balance_infos": [{
                        "currency": "CNY", "total_balance": "12.34",
                        "granted_balance": "2.34", "topped_up_balance": "10.00",
                    }],
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["calls"] += 1
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            return FakeResponse()

        with patch("deepseek_client.urllib.request.urlopen", fake_urlopen):
            first = client.get_balance()
            second = client.get_balance()

        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["url"], "https://api.deepseek.com/user/balance")
        self.assertEqual(captured["authorization"], "Bearer unit-test")
        self.assertEqual(first["balance_infos"][0]["total_balance"], "12.34")
        self.assertEqual(first, second)


class BackendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.config = Config(api_key="test", data_dir=self.temp_path / "data", port=8765)
        cache = TranslationCache(self.temp_path / "cache.sqlite3")
        self.client = StubDeepSeekClient(self.config, cache)
        self.app = ServerApp(self.config, client=self.client)
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, payload=None, origin="chrome-extension://unit-test"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"X-GWT-Client": self.config.client_header}
        if origin is not None:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST" if data is not None else "GET",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers

    def test_health_does_not_disclose_key(self):
        status, payload, _ = self.request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["configured"])
        self.assertNotIn("api_key", json.dumps(payload).lower())
        self.assertNotIn("test", json.dumps(payload))

    def test_web_page_origin_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/v1/translate", sample_payload(), origin="https://evil.example")
        self.assertEqual(context.exception.code, 403)

    def test_extension_origin_can_translate(self):
        status, payload, headers = self.request("/v1/translate", sample_payload())
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["translations"]), 2)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "chrome-extension://unit-test")

    def test_cancel_blocks_followup_batches(self):
        status, payload, _ = self.request("/v1/cancel", {"session_id": "gwt_cancel_12345"})
        self.assertEqual(status, 200)
        followup = sample_payload()
        followup["session_id"] = "gwt_cancel_12345"
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/v1/translate", followup)
        self.assertEqual(context.exception.code, 409)

    def test_extension_preflight_is_allowed_without_sending_secret_header_value(self):
        request = urllib.request.Request(
            self.base + "/v1/translate",
            method="OPTIONS",
            headers={
                "Origin": "chrome-extension://unit-test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-gwt-client",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Private-Network"), "true")

    def test_usage_endpoints_require_extension_origin_and_confirmation(self):
        for path in ("/v1/usage", "/v1/usage/clear"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, {"confirmed": True}, origin="https://evil.example")
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/usage/clear", {})
        self.assertEqual(caught.exception.code, 400)

    def test_usage_and_clear_return_safe_snapshots_preserving_current_page(self):
        self.app.usage.record("paid", "gwt_page_12345", {
            "prompt_tokens": 50, "completion_tokens": 21, "total_tokens": 71,
            "prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 30,
        }, "deepseek-v4-flash", 1789171200)
        _, before, _ = self.request("/v1/usage", {"page_id": "gwt_page_12345"})
        self.assertEqual(before["token_stats"]["history_total_tokens"], 71)
        self.assertGreater(before["token_stats"]["history_total_cost_nano_yuan"], 0)
        _, after, _ = self.request("/v1/usage/clear", {"confirmed": True, "page_id": "gwt_page_12345"})
        self.assertEqual(after["token_stats"]["history_total_tokens"], 0)
        self.assertEqual(after["token_stats"]["history_total_cost_nano_yuan"], 0)
        self.assertEqual(after["token_stats"]["page_total_tokens"], 71)
        self.assertGreater(after["token_stats"]["page_total_cost_nano_yuan"], 0)
        self.assertNotIn("api_key", json.dumps(after))

    def test_usage_requires_client_header_and_rejects_invalid_page_id(self):
        request = urllib.request.Request(
            self.base + "/v1/usage", data=b"{}", headers={"Origin": "chrome-extension://unit-test"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/usage", {"page_id": ["bad"]})
        self.assertEqual(caught.exception.code, 400)

    def test_balance_endpoint_returns_official_balance_without_key(self):
        self.client.get_balance = lambda: {
            "is_available": True,
            "balance_infos": [{
                "currency": "CNY", "total_balance": "8.88",
                "granted_balance": "0.00", "topped_up_balance": "8.88",
            }],
            "fetched_at": 1789747200,
        }
        status, payload, _ = self.request("/v1/balance", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["account_balance"]["balance_infos"][0]["total_balance"], "8.88")
        self.assertNotIn("api_key", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
