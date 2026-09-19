"""Hypothesis generation via an OpenAI-compatible chat endpoint.

Covers Groq and anything else speaking that shape (Together, OpenRouter, a local
vLLM or Ollama server). Deliberately no vendor SDK: stdlib only, so the project
stays dependency-free and the same code reaches every one of them.

This tier has exactly the same standing as every other reasoner. It proposes;
`sell/real/gate.py` decides. A hypothesis from a model is not more trusted than
one from a regex, and the parsing in `hypotheses.py` treats the response as
untrusted input.

Structured output support varies by endpoint and model, so the request degrades
in three steps -- json_schema, then json_object, then a plain instruction with
tolerant extraction -- because a formatting limitation should not be recorded as
a reasoning failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from . import hypotheses
from .sensors import Signal

TIMEOUT = 120


class Transport(Protocol):
    def __call__(self, url: str, payload: dict[str, Any],
                 headers: dict[str, str]) -> tuple[int, str]: ...


def urllib_transport(url: str, payload: dict[str, Any],
                     headers: dict[str, str]) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        # A blocked egress policy surfaces here, not as an HTTP status.
        return 0, json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}})


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    default_model: str
    key_env: str

    def url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


PRESETS: dict[str, Endpoint] = {
    "groq": Endpoint("groq", "https://api.groq.com/openai/v1",
                     "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "together": Endpoint("together", "https://api.together.xyz/v1",
                         "Qwen/Qwen2.5-72B-Instruct-Turbo", "TOGETHER_API_KEY"),
    "openrouter": Endpoint("openrouter", "https://openrouter.ai/api/v1",
                           "qwen/qwen-2.5-72b-instruct", "OPENROUTER_API_KEY"),
    "local": Endpoint("local", "http://127.0.0.1:11434/v1",
                      "llama3.3", "LOCAL_API_KEY"),
}


class OpenAICompatReasoner:
    """Proposes patches using a chat-completions endpoint."""

    def __init__(self, endpoint: str = "groq", *, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 transport: Transport = urllib_transport,
                 fallback: Any = None, temperature: float = 0.0) -> None:
        if endpoint not in PRESETS and base_url is None:
            raise KeyError(f"unknown endpoint {endpoint!r}; "
                           f"choose from {sorted(PRESETS)} or pass base_url")
        preset = PRESETS.get(endpoint, PRESETS["groq"])
        self.endpoint = preset
        self.base_url = base_url or preset.base_url
        self.model = (model or os.environ.get(f"{preset.name.upper()}_MODEL")
                      or preset.default_model)
        self.api_key = api_key or os.environ.get(preset.key_env, "")
        self.transport = transport
        self.temperature = temperature
        self.name = f"{preset.name}:{self.model}"
        self.calls = 0
        self.last_error: str | None = None
        self._format_mode = "json_schema"   # degrades on rejection, then sticks
        if fallback is None:
            from .reasoner import HeuristicReasoner
            fallback = HeuristicReasoner()
        self.fallback = fallback

    # -- availability ----------------------------------------------------

    def available(self) -> str | None:
        if not self.api_key:
            return f"no {self.endpoint.key_env} in the environment"
        return None

    # -- request shaping -------------------------------------------------

    def _response_format(self) -> dict[str, Any] | None:
        if self._format_mode == "json_schema":
            return {"type": "json_schema",
                    "json_schema": {"name": "patch_candidates", "strict": True,
                                    "schema": hypotheses.PATCH_SCHEMA}}
        if self._format_mode == "json_object":
            return {"type": "json_object"}
        return None

    def _payload(self, prompt: str) -> dict[str, Any]:
        system = hypotheses.SYSTEM
        if self._format_mode != "json_schema":
            system += "\n\n" + hypotheses.RESPONSE_INSTRUCTION
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
        }
        fmt = self._response_format()
        if fmt is not None:
            payload["response_format"] = fmt
        return payload

    @staticmethod
    def _content(body: str) -> str | None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else None

    @staticmethod
    def _error(status: int, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return f"HTTP {status}"
        err = data.get("error") or {}
        message = err.get("message") or err.get("type") or f"HTTP {status}"
        return f"HTTP {status}: {str(message)[:200]}"

    # -- the interface every reasoner shares -----------------------------

    def propose(self, signals: list[Signal], ctx: Any) -> list[Any]:
        unavailable = self.available()
        if unavailable:
            self.last_error = unavailable
            return self.fallback.propose(signals, ctx)

        prompt = hypotheses.build_prompt(signals, ctx)
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json",
                   "User-Agent": "sell-engine/0.1"}
        url = self.base_url.rstrip("/") + "/chat/completions"

        # Two attempts at most: one on the current format mode, one after
        # degrading it. A model that cannot honour a schema is a formatting
        # limitation, not a wrong answer.
        for _ in range(2):
            self.calls += 1
            status, body = self.transport(url, self._payload(prompt), headers)
            if status == 200:
                content = self._content(body)
                data = hypotheses.extract_json(content or "")
                if data is None:
                    self.last_error = "response was not usable JSON"
                    break
                patches = hypotheses.to_patches(data, signals, ctx, self.name)
                if not patches:
                    # An explicitly empty candidate list is a real answer: the
                    # model is declining to invent a destination, which the
                    # prompt asks it to do.
                    self.last_error = None
                    return list(self.fallback.propose(signals, ctx))
                self.last_error = None
                return patches + list(self.fallback.propose(signals, ctx))

            self.last_error = self._error(status, body)
            if status == 400 and self._format_mode != "none":
                self._format_mode = ("json_object"
                                     if self._format_mode == "json_schema" else "none")
                continue
            break

        return self.fallback.propose(signals, ctx)


def build(endpoint: str = "groq", **kw: Any) -> OpenAICompatReasoner:
    return OpenAICompatReasoner(endpoint, **kw)
