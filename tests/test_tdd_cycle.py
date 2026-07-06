"""The TDD cycle: test-first policy enforcement and the test-review gate."""

import json

from engorc.agents.schemas import PlanDraft, PlanItemDraft
from engorc.config import Config, IndexConfig, MemoryConfig, RunConfig
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import _draft_to_plan, review_tests
from engorc.orchestrator.services import Services
from engorc.plan import AttemptRecord, WorkItem
from engorc.registry import Registry
from engorc.util import iso_now


def _draft(kind: str, test_first: bool) -> PlanDraft:
    return PlanDraft(reasoning="", goal_recap="", items=[PlanItemDraft(
        title="thing", kind=kind, description="", acceptance=[], verify_commands=[],
        depends_on=[], files_hint=[], size="S", test_first=test_first,
    )])


def test_test_first_policy_enforcement():
    assert "test_first" not in _draft_to_plan(_draft("feature", False), "auto").items[0].notes
    assert "test_first" in _draft_to_plan(_draft("feature", True), "auto").items[0].notes
    assert "test_first" in _draft_to_plan(_draft("feature", False), "always").items[0].notes
    assert "test_first" not in _draft_to_plan(_draft("docs", False), "always").items[0].notes
    assert "test_first" not in _draft_to_plan(_draft("feature", True), "never").items[0].notes


def _services_with_verdict(config: Config, verdict: str, findings: list[dict]) -> Services:
    def brain(messages, response_format, role_model):
        return json.dumps({
            "reasoning": "judging the tests",
            "findings": findings,
            "verdict": verdict,
            "summary": "test suite judged",
        })

    return Services.build(config, client=FakeLLM(brain))


def _tester_setup(tmp_path):
    config = Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        run=RunConfig(review_tests=True),
    )
    registry = Registry(config)
    project = registry.create("tdd mission", title="TDD")
    from engorc.agents.toolbox.git import ensure_repo

    ensure_repo(project.workroom)
    (project.workroom / "test_thing.py").write_text("def test_behavior():\n    assert False\n")
    item = WorkItem(title="build the thing", acceptance=["thing behaves"])
    attempt = AttemptRecord(role="tester", outcome="success", ended=iso_now())
    item.attempts.append(attempt)
    return config, project, item, attempt


def test_good_tests_pass_the_gate(tmp_path):
    config, project, item, attempt = _tester_setup(tmp_path)
    services = _services_with_verdict(config, "approve", [])
    note = review_tests(services, project, item, attempt, sha_before="")
    assert note is None
    assert attempt.outcome == "success"
    assert project.artifacts.exists("review-tests-tests.md", subdir=f"attempts/{item.id}")


def test_weak_tests_are_rejected_with_findings(tmp_path):
    config, project, item, attempt = _tester_setup(tmp_path)
    services = _services_with_verdict(config, "request_changes", [{
        "category": "TEST_GAP", "severity": "blocker",
        "description": "asserts False unconditionally; encodes no behavior",
        "file": "test_thing.py",
        "recommendation": "assert the actual greeting output",
    }])
    note = review_tests(services, project, item, attempt, sha_before="")
    assert note is not None and "test review requested changes" in note
    assert attempt.outcome == "fail"
    assert any("test-review[tests]" in n and "encodes no behavior" in n for n in item.notes)


def test_no_test_changes_fails_the_tester(tmp_path):
    config, project, item, attempt = _tester_setup(tmp_path)
    from engorc.agents.toolbox.git import commit_all

    commit_all(project.workroom, "feat: baseline")  # nothing new after this
    from engorc.agents.toolbox.git import head_sha

    services = _services_with_verdict(config, "approve", [])
    note = review_tests(services, project, item, attempt, sha_before=head_sha(project.workroom))
    assert note is not None and "no test changes" in note
    assert attempt.outcome == "fail"
