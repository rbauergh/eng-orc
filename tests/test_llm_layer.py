"""LLM layer: action parsing, thinking strip, structured repair, packing, config."""

import json

import pytest
from pydantic import BaseModel

from engorc.agents.runtime import FormatError, parse_action
from engorc.agents.schemas import Charter
from engorc.config import Config, RoleModel, load_config
from engorc.llm.budget import ContextPacker, Section
from engorc.llm.fake import FakeLLM
from engorc.llm.structured import (
    StructuredCaller,
    StructuredError,
    extract_json,
    response_format_for,
    strip_thinking,
)


def test_parse_action_happy_path():
    text = (
        "I will write the file now.\n\n"
        'ACTION: write_file {"path": "src/app.py"}\n'
        "```payload\nprint('hi')\n```\n"
    )
    action = parse_action(text)
    assert action.tool == "write_file"
    assert action.args == {"path": "src/app.py"}
    assert action.payload == "print('hi')"
    assert "write the file" in action.thought


def test_parse_action_rejects_zero_and_multiple():
    with pytest.raises(FormatError):
        parse_action("no action here at all")
    with pytest.raises(FormatError):
        parse_action("ACTION: run {}\nACTION: finish {}")
    with pytest.raises(FormatError):
        parse_action('ACTION: run {"broken: json}')


def test_parse_action_code_payload_never_needs_escaping():
    payload = 'data = {"key": "value with \\"quotes\\" and \\n"}\nprint(data)'
    text = f'ACTION: write_file {{"path": "x.py"}}\n```python\n{payload}\n```'
    assert parse_action(text).payload == payload


def test_strip_thinking_all_three_shapes():
    assert strip_thinking("<think>hmm</think>answer") == "answer"
    assert strip_thinking("leaked reasoning</think>answer") == "answer"
    assert strip_thinking("<think>never closed") == ""
    assert strip_thinking("plain") == "plain"


def test_extract_json_from_fence_and_prose():
    assert json.loads(extract_json('```json\n{"a": 1}\n```')) == {"a": 1}
    assert json.loads(extract_json('Sure! Here it is: {"a": {"b": [1, 2]}} hope that helps')) == {
        "a": {"b": [1, 2]}
    }


def test_reasoning_field_comes_first_in_schemas():
    properties = list(Charter.model_json_schema()["properties"])
    assert properties[0] == "reasoning"
    rf = response_format_for(Charter)
    assert rf["schema"] == rf["json_schema"]["schema"]


class Verdict(BaseModel):
    reasoning: str
    ok: bool


def test_structured_caller_repairs_then_succeeds():
    replies = iter(["not json at all", '{"reasoning": "fine", "ok": true}'])
    client = FakeLLM(lambda messages, response_format, role_model: next(replies))
    caller = StructuredCaller(client)
    result = caller.call(RoleModel(model="m"), Verdict, [{"role": "user", "content": "judge"}])
    assert result.ok is True


def test_structured_caller_gives_up_cleanly():
    client = FakeLLM(lambda *a: "garbage forever")
    caller = StructuredCaller(client)
    with pytest.raises(StructuredError):
        caller.call(RoleModel(model="m"), Verdict, [{"role": "user", "content": "judge"}], repair_rounds=1)


def test_packer_respects_budget_and_priorities():
    packer = ContextPacker(context_window=800, reserve_output=100, overhead=0)
    sections = [
        Section(name="Task", text="t " * 300, priority=1),
        Section(name="Fluff", text="f " * 2000, priority=9, min_tokens=50),
    ]
    packed = packer.pack(sections)
    assert packed.tokens <= 700
    assert "Task" in packed.text
    assert packed.truncated or packed.dropped


def test_packer_hard_clamps_priority_one():
    packer = ContextPacker(context_window=400, reserve_output=50, overhead=0)
    packed = packer.pack([Section(name="Huge", text="x " * 5000, priority=1)])
    assert packed.tokens <= 350


def test_config_layering_env_and_yaml(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("run:\n  clarification_budget: 5\n")
    monkeypatch.setenv("ENGORC_HOME", str(home))
    monkeypatch.setenv("ENGORC__RUN__MAX_ATTEMPTS_PER_ITEM", "7")
    monkeypatch.setenv("ENGORC__SERVER__BASE_URL", "http://x:1/v1")
    config = load_config()
    assert config.run.clarification_budget == 5
    assert config.run.max_attempts_per_item == 7
    assert config.server.base_url == "http://x:1/v1"
    assert config.home == home.resolve()


def test_role_model_extra_body_merges_into_payload(monkeypatch):
    from engorc.config import ServerConfig
    from engorc.llm.client import LLMClient

    client = LLMClient(ServerConfig())
    captured: dict = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    def fake_post(payload):
        captured.update(payload)
        return FakeResponse()

    monkeypatch.setattr(client, "_post_chat", fake_post)
    role = RoleModel(
        model="m",
        temperature=0.4,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    client.chat(role, [{"role": "user", "content": "hi"}])
    client.close()
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["model"] == "m"
    assert captured["temperature"] == 0.4
    assert Config().models.coder.extra_body == {}
