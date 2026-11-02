import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.fund_estimate_ai import AiUnavailable, EstimateAi


@pytest.fixture
def model_server():
    state = {
        "finish_reason":"stop",
        "content":json.dumps({"fields":{},"evidence":[{"quote":"a" * 2000}]}),
        "requests": [],
        "reject_max_tokens": False,
        "reject_temperature": False,
        "fail_status": None,
        "fail_body": "",
        "models": ["LongCat-2.5-Flash", "LongCat-2.5-Preview"],
        "models_status": 200,
    }
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["get_path"] = self.path
            state["get_auth"] = self.headers.get("Authorization")
            if state["models_status"] != 200:
                body = json.dumps({"error": {"message": "bad key sk-secret"}}).encode()
            else:
                body = json.dumps({"object": "list", "data": [{"id": name, "object": "model"} for name in state["models"]]}).encode()
            self.send_response(state["models_status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["request"] = payload
            state["requests"].append(payload)
            if state["fail_status"]:
                status = state["fail_status"]
                body = state["fail_body"].encode()
            elif state["reject_max_tokens"] and "max_tokens" in payload:
                status = 400
                body = json.dumps({"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.", "type": "invalid_request_error", "param": "max_tokens", "code": "unsupported_parameter"}}).encode()
            elif state["reject_temperature"] and "temperature" in payload:
                status = 400
                body = json.dumps({"error": {"message": "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.", "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}).encode()
            else:
                status = 200
                body = json.dumps({"id":"chat-1","object":"chat.completion","created":1792713600,"model":"reader","choices":[{"index":0,"message":{"role":"assistant","content":state["content"]},"finish_reason":state["finish_reason"]}],"usage":{"prompt_tokens":500,"completion_tokens":1000,"total_tokens":1500}}).encode()
            self.send_response(status)
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


def test_gpt5_style_models_retry_with_max_completion_tokens(model_server):
    model_server["reject_max_tokens"] = True
    content = ai_for(model_server).complete([{"role":"user","content":"extract"}])
    assert content == model_server["content"]
    first, second = model_server["requests"]
    assert "max_tokens" in first and "max_completion_tokens" not in first
    assert "max_completion_tokens" in second and "max_tokens" not in second
    assert second["max_completion_tokens"] == first["max_tokens"]


def test_reasoning_models_retry_without_temperature(model_server):
    model_server["reject_temperature"] = True
    content = ai_for(model_server).complete([{"role":"user","content":"extract"}])
    assert content == model_server["content"]
    first, second = model_server["requests"]
    assert first["temperature"] == 0
    assert "temperature" not in second


def test_unrelated_bad_request_is_not_retried(model_server):
    model_server["fail_status"] = 400
    model_server["fail_body"] = json.dumps({"error": {"message": "invalid model", "param": "model", "code": "invalid_value"}})
    with pytest.raises(AiUnavailable, match="400"):
        ai_for(model_server).complete([{"role":"user","content":"extract"}])
    assert len(model_server["requests"]) == 1


def test_test_connection_also_adapts_parameters(model_server):
    model_server["reject_max_tokens"] = True
    result = ai_for(model_server).test_connection(model_server["url"], "test-key", "reader")
    assert result["ok"] is True
    assert len(model_server["requests"]) == 2


@pytest.mark.parametrize('body', [
    '<html>proxy login</html>', '{"choices":[]}',
    '{"choices":[{"finish_reason":"length","message":{"content":""}}]}',
    '{"choices":[{"finish_reason":"stop","message":{"content":""}}]}',
])
def test_connection_rejects_http_200_without_complete_text(body):
    ai = EstimateAi(None, poster=lambda *_: (200, body))
    result = ai.test_connection('https://models.example/v1', 'dummy-key', 'reader')
    assert result['ok'] is False


def test_connection_accepts_completed_text_response():
    body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': 'pong'}}]})
    ai = EstimateAi(None, poster=lambda *_: (200, body))
    assert ai.test_connection('https://models.example/v1', 'dummy-key', 'reader')['ok'] is True


def test_list_models_reads_openai_compatible_endpoint(model_server):
    result = ai_for(model_server).list_models(model_server["url"], "test-key")
    assert result["ok"] is True
    assert result["models"] == ["LongCat-2.5-Flash", "LongCat-2.5-Preview"]
    assert model_server["get_path"] == "/v1/models"
    assert model_server["get_auth"] == "Bearer test-key"


def test_list_models_uses_models_suffix_for_v1_base(model_server):
    result = ai_for(model_server).list_models(model_server["url"] + "/v1", "test-key")
    assert result["ok"] is True
    assert model_server["get_path"] == "/v1/models"


def test_list_models_reports_auth_failure_without_leaking_key(model_server):
    model_server["models_status"] = 401
    result = ai_for(model_server).list_models(model_server["url"], "sk-secret")
    assert result["ok"] is False
    assert result["models"] == []
    assert "sk-secret" not in result["message"]
    assert "401" in result["message"]


def test_list_models_requires_key(model_server):
    from src.fund_estimate_ai import AiSettingsError

    with pytest.raises(AiSettingsError, match="API Key"):
        ai_for(model_server).list_models(model_server["url"], "")


def test_list_models_reports_empty_catalog(model_server):
    model_server["models"] = []
    result = ai_for(model_server).list_models(model_server["url"], "test-key")
    assert result["ok"] is False
    assert result["models"] == []
    assert "未返回" in result["message"]


def test_models_url_strips_chat_completions_suffix():
    from src.fund_estimate_ai import models_url

    assert models_url("https://api.longcat.chat/openai/v1/chat/completions") == "https://api.longcat.chat/openai/v1/models"
    assert models_url("https://api.longcat.chat/openai") == "https://api.longcat.chat/openai/v1/models"
    assert models_url("https://api.openai.com/v1") == "https://api.openai.com/v1/models"


def test_list_models_accepts_full_chat_completions_base(model_server):
    result = ai_for(model_server).list_models(model_server["url"] + "/v1/chat/completions", "test-key")
    assert result["ok"] is True
    assert model_server["get_path"] == "/v1/models"


def test_list_models_hints_wrong_base_url_on_404(model_server):
    model_server["models_status"] = 404
    result = ai_for(model_server).list_models(model_server["url"], "test-key")
    assert result["ok"] is False
    assert "404" in result["message"]
    assert "/v1" in result["message"]
