"""Multi-model review panel: seat resolution, lens routing, aggregation, dedupe."""

import json

import pytest

from engorc.config import Config, IndexConfig, MemoryConfig, PanelReviewer, ReviewConfig, RoleModel
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import panel_outcome, run_review_panel
from engorc.orchestrator.services import Services
from engorc.plan import WorkItem
from engorc.registry import Registry


def _approve(summary: str = "fine") -> str:
    return json.dumps({
        "reasoning": "looks right",
        "findings": [],
        "verdict": "approve",
        "summary": summary,
    })


def _block(description: str, category: str = "BUG") -> str:
    return json.dumps({
        "reasoning": "found a problem",
        "findings": [{
            "category": category,
            "severity": "blocker",
            "description": description,
            "file": "greet.py",
            "recommendation": "fix it",
        }],
        "verdict": "request_changes",
        "summary": "needs work",
    })


def _panel_config(tmp_path, panel) -> Config:
    return Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        review=ReviewConfig(panel=panel),
    )


def _services_with_brain(config: Config, brain) -> Services:
    return Services.build(config, client=FakeLLM(brain))


@pytest.fixture()
def item() -> WorkItem:
    return WorkItem(title="Create greet module", acceptance=["greet works"])


def test_panel_runs_each_seat_with_its_lens_and_model(tmp_path, item):
    config = _panel_config(tmp_path, [
        PanelReviewer(model_role="coder", lens="correctness"),
        PanelReviewer(model_role="second", lens="adversarial"),
    ])
    config.models.extra["second"] = RoleModel(model="other-weights", temperature=0.9)

    seen: list[tuple[str, str]] = []

    def brain(messages, response_format, role_model):
        system = messages[0]["content"]
        lens = "adversarial" if "ADVERSARIAL" in system else "correctness"
        seen.append((role_model.model, lens))
        # reviewers must see that the harness already ran verification
        assert "Verification results" in messages[-1]["content"]
        assert "[PASS] python3 -m pytest" in messages[-1]["content"]
        return _approve()

    services = _services_with_brain(config, brain)
    project = Registry(config).create("mission", title="M")
    results = run_review_panel(services, project, item, diff="+ hello",
                               verify_summary="[PASS] python3 -m pytest -q")

    assert seen == [("coder", "correctness"), ("other-weights", "adversarial")]
    approved, blockers = panel_outcome(results)
    assert approved and blockers == []
    review_files = [p.name for p in project.artifacts.list()]
    assert "review-1-correctness.md" in review_files
    assert "review-2-adversarial.md" in review_files


def test_any_blocking_seat_blocks_signoff_and_labels_findings(tmp_path, item):
    config = _panel_config(tmp_path, [
        PanelReviewer(model_role="coder", lens="correctness"),
        PanelReviewer(model_role="coder", lens="adversarial"),
    ])

    def brain(messages, response_format, role_model):
        if "ADVERSARIAL" in messages[0]["content"]:
            return _block("crashes on empty name")
        return _approve()

    services = _services_with_brain(config, brain)
    project = Registry(config).create("mission", title="M")
    results = run_review_panel(services, project, item, diff="+ hello")
    approved, blockers = panel_outcome(results)
    assert not approved
    assert [(lens, f.description) for lens, f in blockers] == [("adversarial", "crashes on empty name")]
    # finding substance travels in the journal so watch/dashboard can show it
    review_events = [e for e in project.journal.iter_events(kinds=["review"])]
    blocking_event = [e for e in review_events if e.payload["verdict"] == "request_changes"]
    assert blocking_event and "crashes on empty name" in blocking_event[0].payload["blockers"][0]


def test_duplicate_findings_across_seats_are_deduped(tmp_path, item):
    config = _panel_config(tmp_path, [
        PanelReviewer(model_role="coder", lens="correctness"),
        PanelReviewer(model_role="coder", lens="adversarial"),
    ])
    services = _services_with_brain(config, lambda *a: _block("Crashes on empty  NAME!"))
    project = Registry(config).create("mission", title="M")
    results = run_review_panel(services, project, item, diff="+ hello")
    _, blockers = panel_outcome(results)
    assert len(blockers) == 1  # same (category, file, normalized description)


def test_dead_seat_is_skipped_and_survivors_decide(tmp_path, item):
    config = _panel_config(tmp_path, [
        PanelReviewer(model_role="ghost-model", lens="correctness"),  # not configured anywhere
        PanelReviewer(model_role="coder", lens="adversarial"),
    ])
    services = _services_with_brain(config, lambda *a: _approve())
    project = Registry(config).create("mission", title="M")
    results = run_review_panel(services, project, item, diff="+ hello")
    assert len(results) == 1
    approved, _ = panel_outcome(results)
    assert approved
    errors = [e for e in project.journal.iter_events(kinds=["error"])]
    assert errors and "ghost-model" in errors[0].payload["error"]


def test_extra_models_resolve_through_for_role():
    config = Config()
    config.models.extra["second-opinion"] = RoleModel(model="coder-fast")
    assert config.models.for_role("second-opinion").model == "coder-fast"
    with pytest.raises(KeyError):
        config.models.for_role("nonexistent")
