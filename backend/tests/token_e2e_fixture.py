"""Local end-to-end fixture: real backend, synthetic DeepSeek HTTP responses."""
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from server import ServerApp, RequestHandler, TranslationHTTPServer
import deepseek_client


class MockResponse:
    def __init__(self, request):
        body = json.loads(request.data)
        self.input = json.loads(body["messages"][1]["content"].split("\n", 1)[1])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        segments = self.input["segments"]
        translated = {"translations": [
            {"id": item["id"], "text": "这段网页内容已经翻译完成。" + str(n)}
            for n, item in enumerate(segments)
        ], "glossary": {}}
        return json.dumps({
            "choices": [{"message": {"content": json.dumps(translated)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": len(segments),
                      "prompt_cache_hit_tokens": 20,
                      "prompt_cache_miss_tokens": 80,
                      "total_tokens": 100 + len(segments)},
            "model": "deepseek-flash",
            "created": 1789171200
        }).encode()


def fake_urlopen(request, timeout):
    if request.full_url != "https://api.deepseek.com/chat/completions":
        raise RuntimeError("Unexpected upstream destination")
    return MockResponse(request)


class FixtureHandler(RequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/fixture":
            return super().do_GET()
        query = parse_qs(parsed.query)
        count = int(query.get("count", ["1"])[0])
        label = query.get("label", ["A"])[0]
        content = ('<!doctype html><html lang="en"><head><title>Test ' + label +
            '</title></head><body><main>' +
            "".join('<p>Article ' + label + ' paragraph number ' + str(n) +
                    ' contains useful information for translation.</p>' for n in range(count)) +
            '</main></body></html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    deepseek_client.urllib.request.urlopen = fake_urlopen
    app = ServerApp(Config(api_key="synthetic-test-only", data_dir=Path(sys.argv[1]), retries=0))
    server = TranslationHTTPServer(("127.0.0.1", int(sys.argv[2])), FixtureHandler, app)
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    server.serve_forever()
