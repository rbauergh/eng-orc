"""End-to-end selftest: the whole orchestrator, no GPU, no servers.

Runs a scripted mission ("greeting CLI") through the real machinery —
registry, charter with a blocking question, gate answer, design, plan,
test-first build with REAL pytest verification in the workroom, review,
commits, wrap-up, memory writes — against the deterministic FakeLLM. Midway
it discards every in-memory object and rebuilds from disk, proving the
resume-from-files contract.

Use it to gain confidence on any machine before models are even downloaded:

    orc selftest            # temp home, cleaned up on success
    orc selftest --keep     # leave the home dir around for inspection
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, IndexConfig, MemoryConfig, RunConfig
from .llm.fake import FakeLLM, assistant_turns, brief_of, role_of, structured_reply
from .obs.console import log
from .orchestrator.scheduler import Scheduler
from .orchestrator.services import Services

MISSION = (
    "Build a tiny greeting tool in Python: a greet(name) function returning "
    "'Hello, <name>!' and a main.py that prints greet('World'). Include pytest tests."
)

PYTEST_CMD = "python3 -m pytest -q --color=no"

_TEST_FILE = '''\
import greet


def test_greet_returns_greeting():
    assert greet.greet("World") == "Hello, World!"


def test_greet_other_name():
    assert greet.greet("Ada") == "Hello, Ada!"
'''

_GREET_FILE = '''\
def greet(name: str) -> str:
    """Return a friendly greeting for name."""
    return f"Hello, {name}!"
'''

_MAIN_FILE = '''\
import greet


def main() -> None:
    print(greet.greet("World"))


if __name__ == "__main__":
    main()
'''

_DESIGN_MD = """\
# Design: Greeting tool

## Objective
A minimal Python package: greet() plus a CLI entry point, verified by pytest.

## Architecture
Two flat modules — greet.py (pure function) and main.py (entry point). No dependencies.

## Stack
Python 3.11+, pytest.

## Components
- greet.py: greet(name) -> str, the only business logic.
- main.py: main() prints greet("World"); __main__ guard.
- test_greet.py: pytest suite encoding the acceptance criteria.

## Data & state
None.

## Test strategy
Unit tests on greet(); main() is exercised by running the script.

## Risks
None meaningful.

## Out of scope
Argument parsing, i18n, packaging.
"""


def _action(tool: str, args: dict | None = None, payload: str = "") -> str:
    line = f"ACTION: {tool}"
    if args:
        line += " " + json.dumps(args)
    text = f"Proceeding.\n\n{line}\n"
    if payload:
        text += f"```payload\n{payload}```\n"
    return text


class DemoBrain:
    """Deterministic responses keyed on schema name, role, and brief content."""

    def __call__(self, messages, response_format, role_model) -> str:
        schema = ""
        if response_format:
            schema = (response_format.get("json_schema") or {}).get("name", "")
        if schema == "Charter":
            return self._charter(messages)
        if schema == "DesignExtract":
            return structured_reply({
                "reasoning": "flat two-module design",
                "decisions": [{
                    "title": "Flat module layout",
                    "decision": "greet.py + main.py at repo root, tests alongside",
                    "rationale": "smallest structure that satisfies the charter",
                    "confidence": 0.9,
                }],
                "stack": ["python", "pytest"],
                "components": ["greet.py: greet()", "main.py: entry point", "test_greet.py: tests"],
                "open_questions": [],
            })
        if schema == "PlanDraft":
            return self._plan()
        if schema == "PlanReviewVerdict":
            return structured_reply({
                "reasoning": "graph is wired, items are crisp and verifiable",
                "findings": [],
                "verdict": "approve",
            })
        if schema == "ReviewVerdict":
            return structured_reply({
                "reasoning": "diff satisfies acceptance; one cosmetic note",
                "findings": [{
                    "category": "STYLE",
                    "severity": "minor",
                    "description": "docstring could mention the exclamation mark",
                    "file": "greet.py",
                    "recommendation": "expand the docstring later",
                }],
                "verdict": "approve",
                "summary": "Meets acceptance criteria; tests are meaningful.",
            })
        if schema == "DigestExtract":
            return structured_reply({
                "reasoning": "short clean run",
                "summary": "Built the greeting tool: charter → design → plan → test-first build, all green.",
                "lessons": ["Writing the failing test before greet.py made verification unambiguous."],
                "conventions": ["User prefers pytest with -q --color=no in automation."],
            })
        return self._freeform(messages)

    def _charter(self, messages) -> str:
        answered = "User answers to earlier questions" in brief_of(messages)
        base = {
            "reasoning": "trivial mission; defaults are obvious",
            "objective": "A greet(name) function plus main.py printing greet('World'), tested with pytest.",
            "context_summary": "Greenfield micro-project, Python, no external dependencies.",
            "assumptions": [
                {"text": "Python 3.11+ with pytest", "confidence": 0.97, "basis": "mission says so"},
                {"text": "flat single-directory layout", "confidence": 0.9, "basis": "project size"},
            ],
            "non_goals": ["argument parsing", "packaging"],
            "success_criteria": [
                "greet('World') returns 'Hello, World!'",
                "python3 main.py prints Hello, World!",
                "pytest passes",
            ],
            "risks": [],
        }
        if answered:
            base["blocking_questions"] = []
            base["ready_to_build"] = True
        else:
            base["blocking_questions"] = [{
                "question": "Should the greeting be English ('Hello') or localized?",
                "why_blocking": "changes the public contract of greet()",
                "options": ["English", "Localized"],
            }]
            base["ready_to_build"] = False
        return structured_reply(base)

    def _plan(self) -> str:
        return structured_reply({
            "reasoning": "two items: core function (test-first), then the CLI",
            "goal_recap": "Deliver greet() with tests, then the CLI entry point.",
            "items": [
                {
                    "title": "Create greet module with greet() function",
                    "kind": "feature",
                    "description": "greet.py exposing greet(name) -> 'Hello, <name>!'",
                    "acceptance": ["greet('World') returns 'Hello, World!'", "pytest passes"],
                    "verify_commands": [PYTEST_CMD],
                    "depends_on": [],
                    "files_hint": ["greet.py", "test_greet.py"],
                    "size": "S",
                    "test_first": True,
                },
                {
                    "title": "Add CLI entry point main.py",
                    "kind": "feature",
                    "description": "main.py printing greet('World') under a __main__ guard",
                    "acceptance": ["python3 main.py prints Hello, World!"],
                    "verify_commands": [PYTEST_CMD, "python3 main.py"],
                    "depends_on": [0],
                    "files_hint": ["main.py"],
                    "size": "S",
                    "test_first": False,
                },
            ],
        })

    def _freeform(self, messages) -> str:
        role = role_of(messages)
        system = messages[0].get("content", "") if messages else ""
        if system.startswith("You compress working notes"):
            return "- compacted earlier progress (selftest)"
        if role.startswith("architect"):
            return _DESIGN_MD
        turn = assistant_turns(messages)
        brief = brief_of(messages)
        if role.startswith("tester"):
            script = [
                _action("write_file", {"path": "test_greet.py"}, _TEST_FILE),
                _action("run_tests"),
                _action("finish", {"status": "done"},
                        "Wrote test_greet.py covering both acceptance criteria; tests fail (red) as expected."),
            ]
            return script[min(turn, len(script) - 1)]
        if role.startswith("implementer"):
            # the task section renders the item title bolded — match that, not the
            # whole brief (the design doc mentions both items)
            if "**Add CLI entry point" in brief:
                script = [
                    _action("write_file", {"path": "main.py"}, _MAIN_FILE),
                    _action("run_tests"),
                    _action("finish", {"status": "done"},
                            "Added main.py printing greet('World'); suite and script both pass."),
                ]
            else:
                script = [
                    _action("write_file", {"path": "greet.py"}, _GREET_FILE),
                    _action("run_tests"),
                    _action("finish", {"status": "done"},
                            "Implemented greet(); the failing tests now pass."),
                ]
            return script[min(turn, len(script) - 1)]
        if role.startswith("scout"):
            return _action("finish", {"status": "done"}, "## What this is\n(selftest scout report)")
        return _action("finish", {"status": "failed"}, f"unscripted role {role!r} in selftest brain")


# ----------------------------------------------------------------------------- checks


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SelftestReport:
    checks: list[Check] = field(default_factory=list)
    home: Path | None = None

    def record(self, name: str, condition: bool, detail: str = "") -> bool:
        self.checks.append(Check(name=name, ok=bool(condition), detail=detail))
        return bool(condition)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _fresh_services(home: Path) -> Services:
    config = Config(
        home=home,
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        # selftest must stay offline-capable and fast; venv creation has its
        # own dedicated tests
        run=RunConfig(project_venvs=False),
    )
    return Services.build(config, client=FakeLLM(DemoBrain()))


def run_selftest(home: Path | None = None, keep: bool = False) -> SelftestReport:
    report = SelftestReport()
    created_temp = home is None
    home = home or Path(tempfile.mkdtemp(prefix="engorc-selftest-"))
    report.home = home

    probe = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True)
    if not report.record("pytest available (needed for real verification)", probe.returncode == 0,
                         "install with: pip install -e '.[dev]'"):
        return report

    try:
        services = _fresh_services(home)
        scheduler = Scheduler(services)
        project = services.registry.create(MISSION, title="Greeting tool")
        slug = project.root.name
        report.record("project created with mission.md", project.mission_path.exists())

        # -- charter round 1: must park on a blocking question ------------------
        scheduler.step(slug)
        project = services.registry.get(slug)
        gates = project.gates.open_gates()
        report.record("charter asked exactly one blocking question", len(gates) == 1,
                      f"open gates: {[g.question for g in gates]}")
        report.record("project parked as blocked_on_user", project.meta.state == "blocked_on_user")
        report.record("scheduler skips parked project", scheduler.step() is None)

        # -- answer the gate asynchronously -----------------------------------------
        if gates:
            project.gates.answer(gates[0].id, "English is fine.")
        report.record("gate answered via inbox", bool(gates))

        # -- charter round 2 → ready; then design --------------------------------
        scheduler.step(slug)
        charter = services.registry.get(slug).charter() or {}
        report.record("charter revision consumed the answer and is ready",
                      charter.get("ready_to_build") is True)
        report.record("assumptions recorded as decisions",
                      len(services.registry.get(slug).decisions.all()) >= 2)

        scheduler.step(slug)
        report.record("design.md written", services.registry.get(slug).design_path.exists())

        scheduler.step(slug)
        plan = services.registry.get(slug).load_plan()
        report.record("plan has 2 items and a valid DAG",
                      len(plan.items) == 2 and not plan.validate_graph())

        # -- first build step: tester writes the failing test ----------------------
        scheduler.step(slug)
        workroom = services.registry.get(slug).workroom
        report.record("test-first: test_greet.py exists before greet.py",
                      (workroom / "test_greet.py").exists() and not (workroom / "greet.py").exists())

        # -- RESUME: throw everything away, rebuild from disk only ------------------
        del services, scheduler, project, plan
        services = _fresh_services(home)
        scheduler = Scheduler(services)
        report.record("fresh process sees the project as runnable",
                      any(p.root.name == slug for p in scheduler.runnable()))

        steps = scheduler.run(max_steps=12)
        project = services.registry.get(slug)
        plan = project.load_plan()
        meta = project.meta
        report.record("mission completed after resume", meta.phase == "done" and meta.state == "done",
                      f"phase={meta.phase} state={meta.state} steps={steps}")
        report.record("all work items done",
                      all(i.status == "done" for i in plan.items),
                      str({i.title: i.status for i in plan.items}))

        verify = subprocess.run([sys.executable, "-m", "pytest", "-q", "--color=no"],
                                cwd=workroom, capture_output=True, text=True)
        report.record("workroom pytest passes for real", verify.returncode == 0,
                      verify.stdout[-300:])
        run_main = subprocess.run([sys.executable, "main.py"], cwd=workroom, capture_output=True, text=True)
        report.record("main.py prints the greeting", run_main.stdout.strip() == "Hello, World!",
                      run_main.stdout)

        git_log = subprocess.run(["git", "-C", str(workroom), "log", "--oneline"],
                                 capture_output=True, text=True)
        commits = [line for line in git_log.stdout.splitlines() if line.strip()]
        report.record("each completed item was committed", len(commits) >= 2, git_log.stdout)

        report.record("final report.md written", project.artifacts.exists("report.md"))
        report.record("review artifact written",
                      any("review" in p.name for p in project.artifacts.list()))

        hits = services.memory.search("failing test verification", k=5)
        report.record("historian saved a lesson to memory", any(h.item.kind == "lesson" for h in hits),
                      str([h.item.title for h in hits]))
        cards = services.memory.search("greeting tool project", k=5, kinds=["project_card"])
        report.record("project card saved to memory", bool(cards))

        # -- multi-project: a second mission coexists and parks independently --------
        second = services.registry.create("Write a CHANGELOG stub", title="Changelog stub")
        report.record("second project schedulable alongside the first",
                      any(p.root.name == second.root.name for p in scheduler.runnable()))
        scheduler.step(second.root.name)
        report.record("second project parked on its own charter question",
                      services.registry.get(second.root.name).meta.state == "blocked_on_user")

        journal_kinds = {e.kind for e in project.journal.iter_events()}
        report.record("journal captured the full lifecycle",
                      {"project_created", "phase_entered", "attempt_finished", "verify_run",
                       "review", "commit"} <= journal_kinds,
                      str(sorted(journal_kinds)))
    except Exception as exc:  # a crash IS a failed selftest, reported not raised
        import traceback

        report.record("selftest crashed", False, f"{exc}\n{traceback.format_exc()[-1500:]}")

    if created_temp and report.ok and not keep:
        shutil.rmtree(home, ignore_errors=True)
        report.home = None
    return report


def print_report(report: SelftestReport) -> None:
    for check in report.checks:
        if check.ok:
            log.success(check.name)
        else:
            log.error(f"{check.name}" + (f" — {check.detail}" if check.detail else ""))
    passed = sum(1 for c in report.checks if c.ok)
    log.rule()
    if report.ok:
        log.success(f"selftest passed ({passed}/{len(report.checks)} checks)")
    else:
        log.error(f"selftest FAILED ({passed}/{len(report.checks)} checks)")
    if report.home is not None:
        log.info(f"selftest home kept at: {report.home}")
