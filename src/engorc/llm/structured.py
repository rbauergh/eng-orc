"""Schema-constrained structured output.

Anything that gates control flow — charters, plans, review verdicts, agent
actions — comes through here. The llama.cpp server compiles the JSON schema
to a grammar, so a conforming byte stream is guaranteed; validation failures
then only ever mean semantic problems, which get one repair round with the
validator errors quoted back to the model.

This replaces the two v0 failure modes: routing decisions parsed out of
prose, and with_structured_output silently accepting degenerate answers.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..config import RoleModel
from ..events import Journal, Kind
from .client import LLMClient, LLMResult, LLMUsage

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredError(Exception):
    def __init__(self, message: str, last_text: str = ""):
        super().__init__(message)
        self.last_text = last_text


def strip_thinking(text: str) -> str:
    """Remove reasoning-model think blocks.

    Handles all three shapes seen from llama.cpp-served reasoning models:
    balanced <think>…</think> blocks, a bare trailing "…</think>" (the chat
    template pre-filled the opener), and an unterminated "<think>…" (the
    model exhausted its output budget mid-thought).
    """
    text = _THINK_RE.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


def extract_json(text: str) -> str:
    """Pull the first balanced JSON object/array out of prose or a code fence."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    start_positions = [i for i, ch in enumerate(text) if ch in "{["]
    for start in start_positions[:3]:
        depth = 0
        in_string = False
        escape = False
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        break
    return text.strip()


def response_format_for(schema: type[BaseModel]) -> dict:
    """Carries the schema in both the llama.cpp form (schema directly under
    response_format) and the OpenAI form (nested under json_schema), so either
    server dialect enforces the grammar; each ignores the other's key."""
    json_schema = schema.model_json_schema()
    return {
        "type": "json_schema",
        "schema": json_schema,
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": json_schema,
        },
    }


def schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "Respond with ONLY a single JSON object matching this JSON Schema — "
        "no prose, no markdown fences:\n"
        + json.dumps(schema.model_json_schema(), indent=1)
    )


class StructuredCaller:
    def __init__(self, client: LLMClient, journal: Journal | None = None, actor: str = "system"):
        self.client = client
        self.journal = journal
        self.actor = actor
        self.usage = LLMUsage()

    def call(
        self,
        role_model: RoleModel,
        schema: type[T],
        messages: list[dict],
        repair_rounds: int = 2,
        max_tokens: int | None = None,
    ) -> T:
        """Returns a validated instance of `schema` or raises StructuredError."""
        convo = list(messages)
        response_format = response_format_for(schema) if role_model.supports_schema else None
        if response_format is None:
            convo = self._with_instructions(convo, schema)

        last_text = ""
        errors = ""
        effective_max = max_tokens if max_tokens is not None else role_model.max_output_tokens
        for round_no in range(repair_rounds + 1):
            result: LLMResult = self.client.chat(
                role_model,
                convo,
                response_format=response_format,
                max_tokens=effective_max,
            )
            self.usage += result.usage
            last_text = result.text
            try:
                # a reasoning model that streamed EVERYTHING into the reasoning
                # channel leaves content empty — the answer often lives there
                candidate = result.text
                if not strip_thinking(candidate).strip() and result.reasoning.strip():
                    candidate = result.reasoning
                parsed = self._parse(role_model, schema, candidate)
                self._journal(schema, ok=True, rounds=round_no + 1, usage=result.usage)
                return parsed
            except (json.JSONDecodeError, ValidationError) as exc:
                errors = str(exc)[:1500]
                if result.finish_reason == "length":
                    # a truncated reply is a budget problem, not a comprehension
                    # problem — quoting validator errors back cannot fix it.
                    # Grow the budget and ask for terser reasoning instead.
                    effective_max = int(effective_max * 1.5)
                    errors = f"reply hit the output token budget before completing; {errors}"
                    repair = (
                        "Your reply was cut off by the output token budget before the JSON "
                        "completed. Respond again with ONLY the JSON object; keep the "
                        "`reasoning` field under 60 words."
                    )
                else:
                    repair = (
                        "That response failed validation:\n"
                        f"{errors}\n\n"
                        "Respond again with ONLY a corrected JSON object matching the schema."
                    )
                convo = convo + [
                    {"role": "assistant", "content": result.text[:4000]},
                    {"role": "user", "content": repair},
                ]
        self._journal(schema, ok=False, rounds=repair_rounds + 1, error=errors)
        raise StructuredError(
            f"{schema.__name__} failed validation after {repair_rounds + 1} rounds: {errors}",
            last_text=last_text,
        )

    def _parse(self, role_model: RoleModel, schema: type[T], text: str) -> T:
        if role_model.thinking:
            text = strip_thinking(text)
        text = text.strip()
        try:
            return schema.model_validate(json.loads(text))
        except json.JSONDecodeError:
            return schema.model_validate(json.loads(extract_json(text)))

    @staticmethod
    def _with_instructions(messages: list[dict], schema: type[BaseModel]) -> list[dict]:
        convo = list(messages)
        suffix = "\n\n" + schema_instructions(schema)
        if convo and convo[0].get("role") == "system":
            convo[0] = {"role": "system", "content": convo[0]["content"] + suffix}
        else:
            convo.insert(0, {"role": "system", "content": suffix})
        return convo

    def _journal(self, schema: type[BaseModel], ok: bool, rounds: int, usage: LLMUsage | None = None, error: str = "") -> None:
        if self.journal is None:
            return
        payload: dict = {"schema": schema.__name__, "ok": ok, "rounds": rounds}
        if usage is not None:
            payload["prompt_tokens"] = usage.prompt_tokens
            payload["completion_tokens"] = usage.completion_tokens
        if error:
            payload["error"] = error[:500]
        self.journal.append(Kind.STRUCTURED_CALL, actor=self.actor, **payload)
