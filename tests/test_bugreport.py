"""Diagnostics report: redaction, content, and doctor-row reuse."""

from engorc.bugreport import build_report, gather_rows, redact
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.services import Services
from engorc.registry import Registry


def test_redact_blanks_secret_strings_but_keeps_numbers():
    data = {
        "server": {"api_key": "hunter2", "base_url": "http://x/v1"},
        "memory": {"letta_token": "tok-abc", "timeout": 30.0},
        "models": {"coder": {"max_output_tokens": 3072, "model": "coder"}},
        "list": [{"secret_sauce": "yes", "n": 1}],
    }
    clean = redact(data)
    assert clean["server"]["api_key"] == "***"
    assert clean["memory"]["letta_token"] == "***"
    assert clean["list"][0]["secret_sauce"] == "***"
    assert clean["models"]["coder"]["max_output_tokens"] == 3072  # 'tokens' != secret
    assert clean["server"]["base_url"] == "http://x/v1"
    assert clean["memory"]["timeout"] == 30.0


def test_build_report_covers_checks_config_and_projects(config):
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    project = Registry(config).create("test mission with [brackets] in it", title="Bracket [proj]")
    project.journal.append("error", error="something broke [badly]")

    report = build_report(services, config)
    assert "# orc diagnostics report" in report
    assert "## Environment checks" in report
    assert "llm server" in report
    assert "## Config (secrets redacted)" in report
    assert "hunter2" not in report and "api_key: '***'" in report or "api_key: ***" in report
    assert project.root.name in report
    assert "something broke [badly]" in report

    rows, _ = gather_rows(services, config)
    labels = [name for name, _, _ in rows]
    assert "llm server" in labels and "memory" in labels
    assert any(label.startswith("model role: coder") for label in labels)
