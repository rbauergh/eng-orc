"""FakeLLM: a deterministic, GPU-less stand-in for the real client.

Implements the same surface as LLMClient, so the entire orchestrator —
registry, phases, tool loop, verification, gates, memory, scheduler — runs
against it unchanged. A Brain callable decides each response from the full
call context; embeddings are hash-derived vectors. Used by `orc selftest`
and the pytest suite; also handy for scripting new scenario tests.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from typing import Protocol

from ..config import RoleModel
from ..util import approx_tokens
from .client import LLMResult, LLMUsage

Brain = Callable[[list[dict], dict | None, RoleModel], str]


class SupportsBrain(Protocol):  # a class with __call__ works too
    def __call__(self, messages: list[dict], response_format: dict | None, role_model: RoleModel) -> str: ...


class FakeLLM:
    def __init__(self, brain: Brain):
        self.brain = brain
        self.calls: list[dict] = []  # inspectable by tests

    # -- LLMClient surface ---------------------------------------------------
    def health(self) -> bool:
        return True

    def model_ids(self) -> list[str]:
        return ["coder", "planner", "utility", "embed"]

    def chat(
        self,
        role_model: RoleModel,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        stream_cb=None,
        extra_body: dict | None = None,
    ) -> LLMResult:
        reply = self.brain(messages, response_format, role_model)
        # a brain may return (text, reasoning) or (text, reasoning, finish_reason)
        # to simulate reasoning-channel models and output-budget truncation
        text, reasoning, finish = reply, "", "stop"
        if isinstance(reply, tuple):
            text = reply[0]
            reasoning = reply[1] if len(reply) > 1 else ""
            finish = reply[2] if len(reply) > 2 else "stop"
        self.calls.append(
            {
                "model": role_model.model,
                "schema": _schema_name(response_format),
                "system_head": (messages[0]["content"].splitlines() or [""])[0] if messages else "",
                "max_tokens": max_tokens,
            }
        )
        if stream_cb is not None:
            stream_cb(text)
        prompt_tokens = sum(approx_tokens(m.get("content", "")) for m in messages)
        return LLMResult(
            text=text,
            reasoning=reasoning,
            model=role_model.model,
            usage=LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=approx_tokens(text)),
            finish_reason=finish,
        )

    def embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        return [_hash_vector(t) for t in texts]

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return approx_tokens(text)

    def close(self) -> None:
        pass


def _schema_name(response_format: dict | None) -> str:
    if not response_format:
        return ""
    nested = response_format.get("json_schema") or {}
    return nested.get("name", "") or response_format.get("type", "")


def _hash_vector(text: str, dim: int = 64) -> list[float]:
    out: list[float] = []
    seed = text.encode("utf-8", errors="replace")
    counter = 0
    while len(out) < dim:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        for offset in range(0, 32, 4):
            (value,) = struct.unpack("<I", digest[offset : offset + 4])
            out.append((value / 0xFFFFFFFF) * 2.0 - 1.0)
            if len(out) >= dim:
                break
        counter += 1
    return out


def structured_reply(payload: dict) -> str:
    return json.dumps(payload)


def role_of(messages: list[dict]) -> str:
    """The system prompts all start '# RoleName' — the cheapest reliable dispatch key."""
    if not messages:
        return ""
    first_line = (messages[0].get("content", "").splitlines() or [""])[0]
    return first_line.lstrip("# ").strip().lower()


def brief_of(messages: list[dict]) -> str:
    return messages[1].get("content", "") if len(messages) > 1 else ""


def assistant_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant")
