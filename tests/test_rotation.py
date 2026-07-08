"""Implementer rotation: a different model family takes over stuck items."""

from engorc.config import Config, IndexConfig, MemoryConfig, RoleModel, RunConfig
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import _run_item_loop, implementer_model_role
from engorc.orchestrator.services import Services
from engorc.plan import AttemptRecord, Plan, WorkItem
from engorc.registry import Registry
from engorc.util import iso_now


def _config_with_fallbacks(tmp_path, fallbacks, max_attempts=3) -> Config:
    config = Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        run=RunConfig(max_attempts_per_item=max_attempts, coder_fallbacks=fallbacks,
                      review_required=False),
    )
    config.models.extra["alt"] = RoleModel(model="alt-weights")
    return config


def test_policy_stays_on_coder_without_fallbacks():
    config = Config(run=RunConfig(coder_fallbacks=[]))
    assert [implementer_model_role(config, n) for n in range(4)] == ["coder"] * 4


def test_policy_rotates_after_primary_attempts_and_clamps():
    config = Config(run=RunConfig(max_attempts_per_item=4, coder_fallbacks=["a", "b"]))
    order = [implementer_model_role(config, n) for n in range(6)]
    assert order == ["coder", "coder", "a", "b", "b", "b"]


def test_tester_failures_do_not_rotate_the_implementer(tmp_path):
    """Regression: the rotation index counted failed attempts from ANY role,
    so a tester stuck on the item pushed the implementer's very first attempt
    onto the fallback family."""
    config = _config_with_fallbacks(tmp_path, ["alt"])
    finish = 'done\n\nACTION: finish {"status": "done"}\n```payload\nfirst try\n```\n'
    client = FakeLLM(lambda *a: finish)
    services = Services.build(config, client=client)
    project = Registry(config).create("mission", title="M")

    item = WorkItem(title="feature with a struggling tester")
    for _ in range(2):  # the TESTER burned attempts, not the implementer
        item.attempts.append(
            AttemptRecord(role="tester", outcome="stuck", ended=iso_now(), model="coder")
        )
    plan = Plan(items=[item])
    project.save_plan(plan)

    attempt, result = _run_item_loop(services, project, plan, item, "implementer")
    assert result.status == "done"
    assert attempt.model == "coder"  # the implementer's own record is clean


def test_stuck_item_attempt_runs_on_the_fallback_family(tmp_path):
    config = _config_with_fallbacks(tmp_path, ["alt"])
    finish = 'done\n\nACTION: finish {"status": "done"}\n```payload\nswitched families and fixed it\n```\n'
    client = FakeLLM(lambda *a: finish)
    services = Services.build(config, client=client)
    project = Registry(config).create("mission", title="M")

    item = WorkItem(title="stubborn feature")
    for _ in range(2):  # the primary coder already burned its attempts
        item.attempts.append(
            AttemptRecord(role="implementer", outcome="fail", ended=iso_now(), model="coder")
        )
    plan = Plan(items=[item])
    project.save_plan(plan)

    attempt, result = _run_item_loop(services, project, plan, item, "implementer")
    assert result.status == "done"
    assert attempt.model == "alt-weights"
    assert client.calls[-1]["model"] == "alt-weights"


def test_unknown_fallback_falls_back_to_primary_coder(tmp_path):
    config = _config_with_fallbacks(tmp_path, ["ghost"])
    config.models.extra.pop("alt")
    finish = 'ok\n\nACTION: finish {"status": "done"}\n```payload\nfine\n```\n'
    services = Services.build(config, client=FakeLLM(lambda *a: finish))
    project = Registry(config).create("mission", title="M")
    item = WorkItem(title="thing")
    for _ in range(2):
        item.attempts.append(
            AttemptRecord(role="implementer", outcome="fail", ended=iso_now(), model="coder")
        )
    plan = Plan(items=[item])
    project.save_plan(plan)
    attempt, result = _run_item_loop(services, project, plan, item, "implementer")
    assert result.status == "done"
    assert attempt.model == "coder"
    errors = list(project.journal.iter_events(kinds=["error"]))
    assert errors and "ghost" in errors[0].payload["error"]
