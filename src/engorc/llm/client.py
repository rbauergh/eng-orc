"""Thin OpenAI-compatible HTTP client for llama.cpp behind llama-swap.

Deliberately httpx-based rather than the openai SDK: we need llama.cpp
extras (grammar-enforced response_format, /tokenize through the proxy's
per-model upstream route) and predictable retry/timeout behavior on a
server that may take tens of seconds to swap a model into VRAM before the
first token appears.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import RoleModel, ServerConfig
from ..util import approx_tokens


class LLMError(Exception):
    pass


class ServerUnavailable(LLMError):
    pass


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __iadd__(self, other: LLMUsage) -> LLMUsage:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        return self


@dataclass
class LLMResult:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str = ""
    elapsed: float = 0.0
    # llama.cpp parses reasoning-model output into a separate channel; some
    # models (gpt-oss, GLM) occasionally put EVERYTHING there, leaving content
    # empty — callers fall back to this when text is blank
    reasoning: str = ""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 503 = llama-swap is still loading/swapping the model; 5xx = transient
        return status >= 500 or status == 429
    return False


class LLMClient:
    def __init__(self, server: ServerConfig):
        self.server = server
        timeout = httpx.Timeout(server.request_timeout, connect=server.connect_timeout)
        self._http = httpx.Client(
            base_url=server.base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {server.api_key}"},
        )
        self._control = httpx.Client(
            base_url=server.control_url.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=server.connect_timeout),
            headers={"Authorization": f"Bearer {server.api_key}"},
        )
        self._retry = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max(1, server.max_retries)),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            reraise=True,
        )

    def close(self) -> None:
        self._http.close()
        self._control.close()

    # -- health / discovery -------------------------------------------------
    def health(self) -> bool:
        try:
            resp = self._http.get("/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def model_ids(self) -> list[str]:
        resp = self._http.get("/models")
        resp.raise_for_status()
        return [m.get("id", "") for m in resp.json().get("data", [])]

    # -- chat ----------------------------------------------------------------
    def chat(
        self,
        role_model: RoleModel,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        stream_cb: Callable[[str], None] | None = None,
        extra_body: dict | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = dict(role_model.extra_body)
        payload.update(
            {
                "model": role_model.model,
                "messages": messages,
                "temperature": role_model.temperature if temperature is None else temperature,
                "top_p": role_model.top_p,
                "max_tokens": role_model.max_output_tokens if max_tokens is None else max_tokens,
            }
        )
        if response_format is not None:
            payload["response_format"] = response_format
        if stop:
            payload["stop"] = stop
        if extra_body:
            payload.update(extra_body)

        started = time.monotonic()
        if stream_cb is not None:
            result = self._retry(self._chat_stream)(payload, stream_cb)
        else:
            result = self._retry(self._chat_once)(payload)
        result.elapsed = time.monotonic() - started
        return result

    def _chat_once(self, payload: dict) -> LLMResult:
        resp = self._post_chat(payload)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResult(
            text=message.get("content") or "",
            reasoning=message.get("reasoning_content") or "",
            model=data.get("model", payload["model"]),
            usage=LLMUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason") or "",
        )

    def _chat_stream(self, payload: dict, stream_cb: Callable[[str], None]) -> LLMResult:
        body = dict(payload)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        chunks: list[str] = []
        reasoning_chunks: list[str] = []
        usage = LLMUsage()
        finish_reason = ""
        model = payload["model"]
        with self._http.stream("POST", "/chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._raise_for_status(resp)
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event.get("usage"):
                    usage = LLMUsage(
                        prompt_tokens=event["usage"].get("prompt_tokens", 0),
                        completion_tokens=event["usage"].get("completion_tokens", 0),
                    )
                for choice in event.get("choices", []):
                    model = event.get("model", model)
                    delta_obj = choice.get("delta") or {}
                    delta = delta_obj.get("content")
                    if delta:
                        chunks.append(delta)
                        stream_cb(delta)
                    reasoning_delta = delta_obj.get("reasoning_content")
                    if reasoning_delta:
                        reasoning_chunks.append(reasoning_delta)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        return LLMResult(text="".join(chunks), reasoning="".join(reasoning_chunks),
                         model=model, usage=usage, finish_reason=finish_reason)

    def _post_chat(self, payload: dict) -> httpx.Response:
        try:
            resp = self._http.post("/chat/completions", json=payload)
        except httpx.ConnectError as exc:
            raise ServerUnavailable(
                f"cannot reach LLM server at {self.server.base_url} — is llama-swap running?"
            ) from exc
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            detail = resp.text[:500]
        except Exception:
            pass
        raise httpx.HTTPStatusError(
            f"LLM server returned {resp.status_code}: {detail}", request=resp.request, response=resp
        )

    # -- embeddings -----------------------------------------------------------
    def embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        base = (self.server.embeddings_url or self.server.base_url).rstrip("/")
        resp = self._retry(self._http.post)(
            f"{base}/embeddings",  # absolute URL overrides the client's base_url
            json={"model": model, "input": texts},
        )
        self._raise_for_status(resp)
        data = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    # -- token counting --------------------------------------------------------
    def count_tokens(self, text: str, model: str | None = None) -> int:
        """Exact count via the proxy's per-model upstream /tokenize when possible,
        pessimistic estimate otherwise."""
        if model:
            try:
                resp = self._control.post(
                    f"/upstream/{model}/tokenize", json={"content": text, "add_special": False}
                )
                if resp.status_code == 200:
                    tokens = resp.json().get("tokens")
                    if isinstance(tokens, list):
                        return len(tokens)
            except httpx.HTTPError:
                pass
        return approx_tokens(text)
