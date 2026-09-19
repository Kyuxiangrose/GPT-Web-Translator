from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cache import TranslationCache
from config import Config
from deepseek_client import DeepSeekClient, RequestCancelled, TranslationError
from token_usage import TokenUsageStore, calculate_cost_nano_yuan
from test_backend import sample_payload
from server import validate_translation_payload


class APIResponse:
    def __init__(self, content=None, usage=None, on_read=None, model="deepseek-flash", created=1789142400):
        self.content = content or json.dumps({"translations": [
            {"id": "s1", "text": "你好"}, {"id": "s2", "text": "了解更多"}]})
        self.usage = {
            "total_tokens": 123, "prompt_tokens": 100, "completion_tokens": 23,
            "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60,
        } if usage is None else usage
        self.on_read = on_read
        self.model = model
        self.created = created

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        if self.on_read:
            self.on_read()
        return json.dumps({
            "choices": [{"message": {"content": self.content}}],
            "usage": self.usage, "model": self.model, "created": self.created,
        }).encode()


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "translations.sqlite3"
        self.config = Config(api_key="test", data_dir=Path(self.temp.name), retries=0)
        self.cache = TranslationCache(self.path)
        self.usage = TokenUsageStore(self.path)
        self.client = DeepSeekClient(self.config, self.cache, self.usage)
        _, self.request = validate_translation_payload(sample_payload(), self.config)
        self.event = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, response=None, page="gwt_page_one"):
        with patch("deepseek_client.urllib.request.urlopen", return_value=response or APIResponse()):
            return self.client.translate(self.request, self.event, page)

    def test_real_total_is_used_even_when_components_do_not_add_up(self):
        self.call(APIResponse(usage={"total_tokens": 777, "prompt_tokens": 1, "completion_tokens": 2}))
        self.assertEqual(self.usage.snapshot("gwt_page_one")["page_total_tokens"], 777)

    def test_cost_uses_cache_breakdown_model_and_peak_period(self):
        usage = {
            "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350,
            "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 200,
        }
        # 2026-09-14 is Monday: 08:00 Beijing is off-peak; 10:00 is peak.
        off_time = datetime(2026, 9, 14, 0, tzinfo=timezone.utc).timestamp()
        peak_time = datetime(2026, 9, 14, 2, tzinfo=timezone.utc).timestamp()
        off_peak, off_version = calculate_cost_nano_yuan(
            usage, "deepseek-v4-flash", off_time
        )
        peak, peak_version = calculate_cost_nano_yuan(
            usage, "deepseek-v4-flash", peak_time
        )
        pro, _ = calculate_cost_nano_yuan(
            usage, "deepseek-v4-pro", off_time
        )
        self.assertEqual(off_peak, 402000)
        self.assertEqual(peak, 804000)
        self.assertEqual(pro, 1590000)
        self.assertTrue(off_version.endswith(":off_peak"))
        self.assertTrue(peak_version.endswith(":peak"))
        alias, alias_version = calculate_cost_nano_yuan(
            usage, "deepseek-flash", off_time
        )
        self.assertEqual(alias, off_peak)
        self.assertIn(":deepseek-flash:", alias_version)
        weekend_time = datetime(2026, 9, 12, 2, tzinfo=timezone.utc).timestamp()
        weekend, weekend_version = calculate_cost_nano_yuan(
            usage, "deepseek-flash", weekend_time
        )
        self.assertEqual(weekend, off_peak)
        self.assertTrue(weekend_version.endswith(":off_peak"))

    def test_invalid_cost_inputs_are_not_guessed(self):
        base = {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
        }
        for usage, model in (
            ({**base, "prompt_cache_miss_tokens": 5}, "deepseek-v4-flash"),
            ({key: value for key, value in base.items() if key != "prompt_cache_hit_tokens"}, "deepseek-v4-flash"),
            (base, "unknown-model"),
        ):
            self.assertEqual(calculate_cost_nano_yuan(usage, model, 1789142400), (None, None))

    def test_cache_hits_including_old_usage_do_not_count(self):
        self.call()
        result = self.call(page="gwt_other_page")
        self.assertTrue(result["cached"])
        self.assertEqual(result["usage"]["total_tokens"], 0)
        self.assertEqual(self.usage.snapshot("gwt_other_page")["page_total_tokens"], 0)
        key = self.client._cache_key(self.request)
        self.cache.put(key, {"translations": [], "usage": {"total_tokens": 999}})
        self.assertEqual(self.call()["usage"]["total_tokens"], 0)
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 123)

    def test_multiple_batches_and_tabs_accumulate_independently(self):
        for n, page in enumerate(["gwt_page_one", "gwt_page_two", "gwt_page_one"]):
            self.request["context"]["previous_text"] = str(n)
            self.call(APIResponse(usage={"total_tokens": [11, 22, 33][n]}), page)
        self.assertEqual(self.usage.snapshot("gwt_page_one")["page_total_tokens"], 44)
        self.assertEqual(self.usage.snapshot("gwt_page_two")["page_total_tokens"], 22)
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 66)

    def test_refresh_old_inflight_response_keeps_original_document_id(self):
        self.call(page="gwt_old_document")
        self.assertEqual(self.usage.snapshot("gwt_new_document")["page_total_tokens"], 0)
        self.usage.record("late", "gwt_old_document", {"total_tokens": 7})
        self.assertEqual(self.usage.snapshot("gwt_new_document")["page_total_tokens"], 0)
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 130)

    def test_http_errors_are_excluded_including_error_body_usage(self):
        for status in (401, 429, 500):
            error = urllib.error.HTTPError("https://api.deepseek.com", status, "error", {},
                io.BytesIO(b'{"usage":{"total_tokens":999}}'))
            with patch("deepseek_client.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(TranslationError):
                    self.client.translate(self.request, self.event, "gwt_page_one")
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 0)

    def test_successful_response_with_invalid_translation_still_counts(self):
        with self.assertRaises(TranslationError):
            self.call(APIResponse(content="invalid JSON"))
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 123)

    def test_successful_but_invalid_first_attempt_and_retry_both_count(self):
        config = Config(api_key="test", retries=1)
        client = DeepSeekClient(config, self.cache, self.usage)
        with patch("deepseek_client.urllib.request.urlopen", side_effect=[
            APIResponse(content="invalid"), APIResponse(usage={"total_tokens": 321})
        ]):
            client.translate(self.request, self.event, "gwt_page_one")
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 444)

    def test_http_failure_then_success_counts_only_success(self):
        client = DeepSeekClient(Config(api_key="test", retries=1), self.cache, self.usage)
        with patch("deepseek_client.urllib.request.urlopen", side_effect=[
            urllib.error.HTTPError("https://api.deepseek.com", 503, "error", {}, io.BytesIO(b"")),
            APIResponse()
        ]):
            client.translate(self.request, self.event, "gwt_page_one")
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 123)

    def test_cancel_after_successful_api_response_does_not_lose_usage(self):
        with self.assertRaises(RequestCancelled):
            self.call(APIResponse(on_read=self.event.set))
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 123)

    def test_cancel_before_api_does_not_count(self):
        self.event.set()
        with self.assertRaises(RequestCancelled):
            self.call()
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 0)

    def test_missing_or_invalid_usage_is_not_estimated(self):
        values = [{}, {"prompt_tokens": 9, "completion_tokens": 8},
                  {"total_tokens": "17"}, {"total_tokens": -1},
                  {"total_tokens": True}, {"total_tokens": 1.5}, {"total_tokens": None}]
        for n, usage in enumerate(values):
            self.request["context"]["previous_text"] = str(n)
            self.call(APIResponse(usage=usage))
        stats = self.usage.snapshot()
        self.assertEqual(stats["history_total_tokens"], 0)
        self.assertEqual(stats["history_missing_usage"], len(values))

    def test_duplicate_receipts_are_idempotent_even_after_clear(self):
        self.usage.record("same-id", "gwt_page_one", {"total_tokens": 5})
        self.assertFalse(self.usage.record("same-id", "gwt_page_one", {"total_tokens": 5}))
        self.usage.clear_history()
        self.assertFalse(self.usage.record("same-id", "gwt_page_one", {"total_tokens": 5}))
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 0)
        self.assertEqual(self.usage.snapshot("gwt_page_one")["page_total_tokens"], 5)

    def test_concurrent_writes_and_duplicate_deliveries_do_not_lose_increments(self):
        def record(n):
            store = TokenUsageStore(self.path)
            store.record(str(n % 40), "page_" + str(n % 4), {"total_tokens": 3})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(80)))
        self.assertEqual(self.usage.snapshot()["history_total_tokens"], 120)
        for n in range(4):
            self.assertEqual(self.usage.snapshot("page_" + str(n))["page_total_tokens"], 30)

    def test_clear_preserves_page_cache_and_counts_later_completions(self):
        self.call()
        before = self.usage.snapshot()
        self.usage.clear_history()
        self.usage.record("late-response", "gwt_page_one", {"total_tokens": 8})
        after = self.usage.snapshot("gwt_page_one")
        self.assertEqual(after["history_total_tokens"], 8)
        self.assertEqual(after["page_total_tokens"], 131)
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(self.cache.count(), 1)

    def test_clear_resets_history_tokens_and_cost_together_but_keeps_page(self):
        self.call()
        before = self.usage.snapshot("gwt_page_one")
        self.assertGreater(before["history_total_cost_nano_yuan"], 0)
        self.usage.clear_history()
        after = self.usage.snapshot("gwt_page_one")
        self.assertEqual(after["history_total_tokens"], 0)
        self.assertEqual(after["history_total_cost_nano_yuan"], 0)
        self.assertEqual(after["history_missing_cost"], 0)
        self.assertEqual(after["page_total_tokens"], before["page_total_tokens"])
        self.assertEqual(after["page_total_cost_nano_yuan"], before["page_total_cost_nano_yuan"])

    def test_v103_database_migrates_old_money_as_unknown(self):
        old_path = Path(self.temp.name) / "old.sqlite3"
        db = sqlite3.connect(old_path)
        try:
            db.execute("""CREATE TABLE token_history (
                singleton INTEGER PRIMARY KEY, store_id TEXT, revision INTEGER,
                total_tokens INTEGER, missing_usage INTEGER, started_at INTEGER)""")
            db.execute("INSERT INTO token_history VALUES (1, 'old', 1, 3186, 0, 1)")
            db.execute("""CREATE TABLE token_pages (
                page_id TEXT PRIMARY KEY, total_tokens INTEGER, missing_usage INTEGER)""")
            db.execute("INSERT INTO token_pages VALUES ('gwt_old_page', 3186, 0)")
            db.execute("""CREATE TABLE token_events (
                event_id TEXT PRIMARY KEY, page_id TEXT, total_tokens INTEGER, recorded_at INTEGER)""")
            db.execute("INSERT INTO token_events VALUES ('old-event', 'gwt_old_page', 3186, 1)")
            db.commit()
        finally:
            db.close()
        migrated = TokenUsageStore(old_path).snapshot("gwt_old_page")
        self.assertEqual(migrated["history_total_tokens"], 3186)
        self.assertEqual(migrated["history_total_cost_nano_yuan"], 0)
        self.assertEqual(migrated["history_missing_cost"], 1)
        self.assertEqual(migrated["page_missing_cost"], 1)

    def test_saved_known_response_is_repriced_once(self):
        old_path = Path(self.temp.name) / "alias.sqlite3"
        db = sqlite3.connect(old_path)
        try:
            db.execute("""CREATE TABLE token_history (
                singleton INTEGER PRIMARY KEY, store_id TEXT, revision INTEGER,
                total_tokens INTEGER, missing_usage INTEGER, started_at INTEGER,
                total_cost_nano_yuan INTEGER, missing_cost INTEGER)""")
            db.execute("INSERT INTO token_history VALUES (1, 'old', 1, 350, 0, 1, 530000, 0)")
            db.execute("""CREATE TABLE token_pages (
                page_id TEXT PRIMARY KEY, total_tokens INTEGER, missing_usage INTEGER,
                total_cost_nano_yuan INTEGER, missing_cost INTEGER)""")
            db.execute("INSERT INTO token_pages VALUES ('gwt_alias_page', 350, 0, 530000, 0)")
            db.execute("""CREATE TABLE token_events (
                event_id TEXT PRIMARY KEY, page_id TEXT, total_tokens INTEGER,
                recorded_at INTEGER, cost_nano_yuan INTEGER, pricing_version TEXT,
                model TEXT, prompt_cache_hit_tokens INTEGER,
                prompt_cache_miss_tokens INTEGER, completion_tokens INTEGER)""")
            db.execute("""INSERT INTO token_events VALUES (
                'alias-event', 'gwt_alias_page', 350, 1789171200,
                530000, 'deepseek-cn-2026-08-17:deepseek-v4-flash:off_peak',
                'deepseek-v4-flash', 100, 200, 50)""")
            db.commit()
        finally:
            db.close()
        first = TokenUsageStore(old_path).snapshot("gwt_alias_page")
        second = TokenUsageStore(old_path).snapshot("gwt_alias_page")
        self.assertEqual(first["history_total_cost_nano_yuan"], 402000)
        self.assertEqual(first["page_total_cost_nano_yuan"], 402000)
        self.assertEqual(first["history_missing_cost"], 0)
        self.assertEqual(first, second)

    def test_reopening_database_preserves_history_and_page_totals(self):
        self.call()
        fresh_store = TokenUsageStore(self.path)
        self.assertEqual(fresh_store.snapshot("gwt_page_one"), self.usage.snapshot("gwt_page_one"))

    def test_storage_failure_does_not_retry_paid_api(self):
        client = DeepSeekClient(Config(api_key="test", retries=2), self.cache, self.usage)
        with patch("deepseek_client.urllib.request.urlopen", return_value=APIResponse()) as upstream:
            with patch.object(self.usage, "record", side_effect=sqlite3.OperationalError("disk full")):
                with self.assertRaises(TranslationError) as caught:
                    client.translate(self.request, self.event, "gwt_page_one")
        self.assertEqual(caught.exception.code, "usage_storage_error")
        self.assertEqual(upstream.call_count, 1)


if __name__ == "__main__":
    unittest.main()
