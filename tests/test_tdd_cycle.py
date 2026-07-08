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


def test_red_check_executes_the_suite_mechanically(tmp_path):
    """TDD's core fact is executed, not asserted: red suites report red,
    green suites report green, no suite reports nothing."""
    from engorc.orchestrator.phases import _red_check
    from engorc.plan import WorkItem as WI

    config, project, item, _ = _tester_setup(tmp_path)  # test_thing.py asserts False
    services = _services_with_verdict(config, "approve", [])
    all_green, execution = _red_check(services, project, item)
    assert all_green is False
    assert "test_thing" in execution or "FAIL" in execution

    (project.workroom / "test_thing.py").write_text("def test_behavior():\n    assert True\n")
    all_green, _ = _red_check(services, project, item)
    assert all_green is True

    bare = _tester_setup(tmp_path / "second")[1]
    (bare.workroom / "test_thing.py").unlink()
    assert _red_check(services, bare, WI(title="x")) == (None, "")


def test_execution_evidence_reaches_the_test_reviewer(tmp_path):
    """The reviewer judges red/green MEANING; the harness supplies the fact."""
    import json as _json

    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.services import Services

    config, project, item, attempt = _tester_setup(tmp_path)
    seen: list[str] = []

    def brain(messages, response_format, role_model):
        seen.append(messages[-1]["content"])
        return _json.dumps({"reasoning": "judged", "findings": [],
                            "verdict": "approve", "summary": "ok"})

    services = Services.build(config, client=FakeLLM(brain))
    note = review_tests(services, project, item, attempt, sha_before="",
                        execution_summary="TEST EXECUTION AFTER THE TESTER FINISHED: RED — 3 failed")
    assert note is None
    assert any("TEST EXECUTION AFTER THE TESTER FINISHED: RED" in c for c in seen)


def test_no_new_tests_is_exempt_from_the_red_check(tmp_path):
    """A tester that validly wrote nothing (the suite already covers the
    item) must NOT get the 'green = possibly vacuous' treatment — green is
    simply expected there."""
    import json as _json

    from engorc.config import Config, IndexConfig, MemoryConfig, RunConfig
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.phases import phase_build
    from engorc.orchestrator.services import Services
    from engorc.plan import Plan
    from engorc.registry import Registry

    config = Config(home=tmp_path / "home", memory=MemoryConfig(backend="local"),
                    index=IndexConfig(enabled=False),
                    run=RunConfig(project_venvs=False, review_tests=True))
    project = Registry(config).create("covered mission", title="C")
    from engorc.agents.toolbox.git import commit_all, ensure_repo

    ensure_repo(project.workroom)
    (project.workroom / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    commit_all(project.workroom, "feat: suite in place")

    item = WorkItem(title="already covered thing", notes=["test_first"])
    project.save_plan(Plan(items=[item]))

    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        if name:
            return _json.dumps({"reasoning": "n/a", "findings": [],
                                "verdict": "approve", "summary": "ok"})
        return ('done\n\nACTION: finish {"status": "done"}\n'
                "```payload\nsuite already covers this item\n```\n")

    services = Services.build(config, client=FakeLLM(brain))
    note = phase_build(services, project)
    assert "existing tests already cover" in note
    refreshed = project.load_plan().get(item.id)
    tester_attempt = refreshed.attempts[-1]
    assert tester_attempt.test_summary == ""  # no red/green framing applied
    red_checks = list(project.journal.iter_events(kinds=["verify_run"]))
    assert not red_checks  # the suite was not executed as a red check


def test_no_changes_with_an_existing_suite_is_accepted(tmp_path):
    """Regression: the tester correctly reported 'the suite already covers
    this item' (as its prompt instructs) and the gate failed it for producing
    no diff — the gate contradicted the prompt."""
    config, project, item, attempt = _tester_setup(tmp_path)
    from engorc.agents.toolbox.git import commit_all, head_sha

    commit_all(project.workroom, "feat: baseline with tests")  # nothing new after this
    services = _services_with_verdict(config, "approve", [])
    note = review_tests(services, project, item, attempt, sha_before=head_sha(project.workroom))
    assert note is None  # proceed to implementation
    assert attempt.outcome == "success"
    assert any("already covers" in n for n in item.notes)


def test_no_changes_and_no_tests_still_fails_the_tester(tmp_path):
    config, project, item, attempt = _tester_setup(tmp_path)
    from engorc.agents.toolbox.git import commit_all, head_sha

    (project.workroom / "test_thing.py").unlink()  # a truly lazy finish
    commit_all(project.workroom, "chore: baseline without tests")
    services = _services_with_verdict(config, "approve", [])
    note = review_tests(services, project, item, attempt, sha_before=head_sha(project.workroom))
    assert note is not None and "no test changes" in note
    assert attempt.outcome == "fail"
