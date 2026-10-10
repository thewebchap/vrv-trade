"""The model wrapper must never raise: every failure becomes an error string."""
import requests

from tradepilot.local_model import LocalModel, parse_json_object


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _model(tmp_path):
    return LocalModel(name="qwen-test", base_url="http://127.0.0.1:9", timeout_s=2, cache_dir=str(tmp_path))


def test_server_unreachable(tmp_path):
    r = _model(tmp_path).ask_json("hi")
    assert r.data is None and "not reachable" in r.error


def test_timeout(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr(requests, "post", boom)
    assert "timed out" in _model(tmp_path).ask_json("hi").error


def test_model_not_pulled(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(404, text="model not found"))
    assert "ollama pull qwen-test" in _model(tmp_path).ask_json("hi").error


def test_invalid_json_content(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"message": {"content": "Sure! {bad json"}}))
    r = _model(tmp_path).ask_json("hi")
    assert r.data is None and "not valid JSON" in r.error


def test_empty_content_from_thinking_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"message": {"content": "", "thinking": "..."}}))
    assert "empty content" in _model(tmp_path).ask_json("hi").error


def test_truncated_output(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"message": {"content": '{"a": 1}'}, "done_reason": "length"}))
    assert "truncated" in _model(tmp_path).ask_json("hi").error


def test_success_is_cached(tmp_path, monkeypatch):
    calls = []

    def post(*a, **k):
        calls.append(1)
        return _Resp(200, {"message": {"content": '{"answer": "YES"}'}})
    monkeypatch.setattr(requests, "post", post)
    m = _model(tmp_path)
    assert m.ask_json("q").data == {"answer": "YES"}
    second = m.ask_json("q")
    assert second.cached and len(calls) == 1


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1}\n```')[0] == {"a": 1}
    assert parse_json_object("[1, 2]")[1] == "model output is JSON but not an object"
