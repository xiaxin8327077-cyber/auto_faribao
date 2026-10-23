import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.fund_estimate_ai import AiUnavailable, EstimateAi


@pytest.fixture
def model_server():
    state = {"finish_reason":"stop", "content":json.dumps({"fields":{},"evidence":[{"quote":"a" * 2000}]})}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"id":"chat-1","object":"chat.completion","created":1792713600,"model":"reader","choices":[{"index":0,"message":{"role":"assistant","content":state["content"]},"finish_reason":state["finish_reason"]}],"usage":{"prompt_tokens":500,"completion_tokens":1000,"total_tokens":1500}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    worker.join()


def ai_for(state):
    class Store:
        def secret_ai_settings(self):
            return {"enabled":True,"base_url":state["url"],"api_key":"test-key","model":"reader"}
    return EstimateAi(Store())


def test_real_http_shape_accepts_complete_structured_output(model_server):
    content = ai_for(model_server).complete([{"role":"user","content":"extract"}])
    assert content == model_server["content"]
    assert model_server["request"]["max_tokens"] >= 2048


def test_real_http_shape_rejects_length_finish_reason(model_server):
    model_server["finish_reason"] = "length"
    model_server["content"] = '{"fields":{}}'
    with pytest.raises(AiUnavailable, match="截断"):
        ai_for(model_server).complete([{"role":"user","content":"extract"}])
