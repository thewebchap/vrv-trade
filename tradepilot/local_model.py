"""The only module that talks to the local model.

Contract: `ask_json()` never raises. It returns a ModelResponse whose `data`
is a dict on success, or None with `error` explaining why (server down,
model missing, timeout, invalid JSON...). Callers turn errors into
REVIEW. Responses are cached on disk keyed by model + prompt +
images, so re-runs are fast and reproducible.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass

import requests

from .config import CONFIG

log = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    data: dict | None
    error: str | None = None
    model: str = ""
    cached: bool = False


class LocalModel:
    def __init__(self, name: str | None = None, base_url: str | None = None,
                 timeout_s: int | None = None, cache_dir: str | None = None):
        self.name = name or CONFIG.model_name
        self.base_url = (base_url or CONFIG.ollama_base_url).rstrip("/")
        self.timeout_s = timeout_s or CONFIG.model_timeout_s
        self.cache_dir = os.path.join(cache_dir or CONFIG.cache_dir, "model")
        if CONFIG.model_provider != "ollama":
            raise ValueError(f"Unsupported LOCAL_MODEL_PROVIDER={CONFIG.model_provider!r} (only 'ollama')")

    # -- public -------------------------------------------------------------
    def ask_json(self, prompt: str, images: list[bytes] | None = None,
                 max_tokens: int = 1200) -> ModelResponse:
        key = self._cache_key(prompt, images)
        cached = self._cache_get(key)
        if cached is not None:
            return ModelResponse(cached, model=self.name, cached=True)

        raw, error = self._call(prompt, images, max_tokens)
        if error:
            log.warning("[MODEL] %s", error)
            return ModelResponse(None, error, self.name)
        data, error = parse_json_object(raw)
        if error:
            log.warning("[MODEL] invalid response: %s", error)
            return ModelResponse(None, error, self.name)
        self._cache_put(key, data)
        return ModelResponse(data, model=self.name)

    # -- internals ----------------------------------------------------------
    def _call(self, prompt: str, images: list[bytes] | None, max_tokens: int) -> tuple[str | None, str | None]:
        msg = {"role": "user", "content": prompt}
        if images:
            msg["images"] = [base64.b64encode(i).decode() for i in images]
        body = {
            "model": self.name, "messages": [msg], "format": "json", "stream": False,
            # temperature 0 for repeatability; num_predict caps runaway generations
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": CONFIG.model_num_ctx},
        }
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout_s)
        except requests.exceptions.ConnectionError:
            return None, f"Local model server not reachable at {self.base_url} (is 'ollama serve' running?)"
        except requests.exceptions.Timeout:
            return None, f"Local model timed out after {self.timeout_s}s"
        except requests.exceptions.RequestException as e:
            return None, f"Local model request failed: {e}"
        if r.status_code == 404:
            return None, f"Model '{self.name}' not found in Ollama (run: ollama pull {self.name})"
        if r.status_code != 200:
            return None, f"Local model HTTP {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except ValueError:
            return None, "Local model returned a non-JSON HTTP body"
        content = (payload.get("message") or {}).get("content") or ""
        if not content.strip():
            return None, "Local model returned empty content (is this a 'thinking' model variant?)"
        if payload.get("done_reason") == "length":
            return None, "Local model output was truncated (max tokens reached)"
        return content, None

    def _cache_key(self, prompt: str, images: list[bytes] | None) -> str:
        h = hashlib.sha256(self.name.encode() + b"\0" + prompt.encode())
        for img in images or []:
            h.update(hashlib.sha256(img).digest())
        return h.hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        path = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None

    def _cache_put(self, key: str, data: dict) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, key + ".json"), "w") as f:
            json.dump(data, f)


def parse_json_object(raw: str | None) -> tuple[dict | None, str | None]:
    if raw is None:
        return None, "no content"
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"model output is not valid JSON ({e.msg})"
    if not isinstance(data, dict):
        return None, "model output is JSON but not an object"
    return data, None
