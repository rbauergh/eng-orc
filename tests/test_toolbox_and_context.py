"""Toolbox safety and context machinery, exercised against real files."""

import pytest

from engorc.agents.toolbox import ALL_TOOLS, ToolContext, run_verification
from engorc.agents.toolbox.git import commit_all, ensure_repo, head_sha
from engorc.context.repomap import RepoMap
from engorc.context.retriever import HybridRetriever, extract_terms
from engorc.events import Journal


@pytest.fixture()
def ctx(config, project):
    return ToolContext(
        project=project,
        config=config,
        journal=Journal(project.root / "journal"),
        item_id="wi_test",
        role="implementer",
    )


def tool(name):
    return ALL_TOOLS[name]


def test_path_jail_blocks_escapes(ctx):
    result = tool("read_file").run(ctx, {"path": "../../project.json"}, "")
    assert not result.ok and "denied" in result.output
    result = tool("write_file").run(ctx, {"path": "/etc/evil"}, "x")
    assert not result.ok


def test_binary_workroom_content_never_crashes_decoding(ctx):
    """Regression: an implementer generated NUL-free binary .wav files; git's
    text heuristic dumped all 16MB into the diff and the strict utf-8 decode
    killed the whole step ('invalid continuation byte')."""
    from engorc.agents.toolbox.git import diff_since, ensure_repo, head_sha
    from engorc.agents.toolbox.shell import run_command

    workroom = ctx.workroom
    ensure_repo(workroom)
    # RIFF header + high bytes, no NULs: git treats it as text, utf-8 can't
    (workroom / "drop.wav").write_bytes(b"RIFF\x24\x08WAVE" + b"\xdc\xff\xfe\xdc" * 64)
    diff = diff_since(workroom, head_sha(workroom))
    assert "drop.wav" in diff  # decoded with replacement, not a crash

    result = run_command(ctx, r"printf '\xdc\xff\xfe'", timeout=30)
    assert result.ok and "exit code 0" in result.output


def test_step_errors_carry_the_crash_site(config, monkeypatch):
    """Regression: '[system] utf-8 codec can't decode…' gave no clue WHERE —
    step errors now journal the exception type and deepest frame."""
    import engorc.orchestrator.scheduler as scheduler_module
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.scheduler import Scheduler
    from engorc.orchestrator.services import Services
    from engorc.registry import Registry

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    project = Registry(config).create("crashy mission", title="C")

    def boom(services, project):
        raise UnicodeDecodeError("utf-8", b"\xdc", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(scheduler_module, "run_step", boom)
    slug, note = Scheduler(services).step(project.root.name)
    assert "step errored" in note
    errors = list(project.journal.iter_events(kinds=["error"]))
    assert errors and "UnicodeDecodeError" in errors[-1].payload["error"]
    assert "at File" in errors[-1].payload["error"] or "(at " in errors[-1].payload["error"]


def test_write_and_windowed_read(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 301))
    assert tool("write_file").run(ctx, {"path": "big.txt"}, body).ok
    view = tool("read_file").run(ctx, {"path": "big.txt", "start": 150}, "")
    assert view.ok and "150| line 150" in view.output and "line 300" not in view.output


def test_python_syntax_gate_blocks_bad_writes_and_edits(ctx):
    bad = tool("write_file").run(ctx, {"path": "mod.py"}, "def broken(:\n    pass")
    assert not bad.ok and "syntax error" in bad.output
    assert tool("write_file").run(ctx, {"path": "mod.py"}, "def fine():\n    return 1").ok
    edit = tool("edit_file").run(
        ctx,
        {"path": "mod.py"},
        "<<<<<<< SEARCH\ndef fine():\n=======\ndef fine(:\n>>>>>>> REPLACE",
    )
    assert not edit.ok and "NOT applied" in edit.output
    assert (ctx.workroom / "mod.py").read_text().startswith("def fine():")


def test_edit_requires_unique_exact_match(ctx):
    tool("write_file").run(ctx, {"path": "dup.py"}, "x = 1\ny = 2\nx = 1")
    ambiguous = tool("edit_file").run(
        ctx, {"path": "dup.py"}, "<<<<<<< SEARCH\nx = 1\n=======\nx = 9\n>>>>>>> REPLACE"
    )
    assert not ambiguous.ok and "2 times" in ambiguous.output
    missing = tool("edit_file").run(
        ctx, {"path": "dup.py"}, "<<<<<<< SEARCH\nz = 0\n=======\nz = 1\n>>>>>>> REPLACE"
    )
    assert not missing.ok and "not found" in missing.output


def test_run_denylist_and_execution(ctx):
    assert not tool("run").run(ctx, {}, "sudo rm -rf /").ok
    assert not tool("run").run(ctx, {}, "git push origin main").ok
    result = tool("run").run(ctx, {}, "echo hello && echo world")
    assert result.ok and "hello" in result.output and "world" in result.output
    failing = tool("run").run(ctx, {}, "exit 3")
    assert not failing.ok and "exit code 3" in failing.output


def test_run_tests_detects_pytest_and_reports(ctx):
    (ctx.workroom / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    report = run_verification(ctx, None)
    assert report.passed and "PASS" in report.summary()
    (ctx.workroom / "test_sample.py").write_text("def test_bad():\n    assert False\n")
    report = run_verification(ctx, None)
    assert not report.passed and report.failure_detail()


def test_verify_commands_run_in_order_and_stop_on_failure(ctx):
    report = run_verification(ctx, ["echo one", "exit 1", "echo never"])
    assert not report.passed
    assert [r.exit_code for r in report.results] == [0, 1]


def test_grep_python_fallback_and_caps(ctx, monkeypatch):
    import shutil as _shutil

    (ctx.workroom / "alpha.py").write_text("def needle_function():\n    pass\n")
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    result = tool("grep").run(ctx, {"pattern": "needle_function"}, "")
    assert result.ok and "alpha.py:1" in result.output


def test_git_helpers_commit_cycle(ctx):
    workroom = ctx.workroom
    assert ensure_repo(workroom)
    (workroom / "a.txt").write_text("one\n")
    ok, sha = commit_all(workroom, "feat: first")
    assert ok and sha
    assert head_sha(workroom) == sha
    ok, message = commit_all(workroom, "feat: nothing new")
    assert ok and message == "nothing to commit"


def test_finish_and_ask_user_control_tools(ctx):
    done = tool("finish").run(ctx, {"status": "done"}, "all good")
    assert done.data["terminal"] == "done"
    missing_note = tool("finish").run(ctx, {"status": "done"}, "")
    assert not missing_note.ok
    parked = tool("ask_user").run(ctx, {}, "Which port should the server use?")
    assert parked.data["terminal"] == "blocked_on_user"
    gates = ctx.project.gates.open_gates()
    assert gates and "port" in gates[0].question


def test_repomap_python_tags_render_and_cache(ctx, config):
    workroom = ctx.workroom
    (workroom / "engine.py").write_text(
        "class Engine:\n    def start(self):\n        pass\n\n\ndef helper():\n    pass\n"
    )
    repomap = RepoMap(workroom, ctx.project.index_dir, config.index.ignore)
    rendered = repomap.render(budget_tokens=400)
    assert "engine.py" in rendered and "Engine" in rendered
    assert repomap.find_symbol("Engine")
    rendered_again = repomap.render(budget_tokens=400)
    assert "Engine" in rendered_again  # served from cache without error


def test_retriever_gathers_without_index(ctx, config):
    workroom = ctx.workroom
    (workroom / "config_loader.py").write_text(
        "def load_settings(path):\n    return {'path': path}\n"
    )
    repomap = RepoMap(workroom, ctx.project.index_dir, config.index.ignore)
    retriever = HybridRetriever(workroom, config, repomap, index=None)
    assert "load_settings" in extract_terms("how does load_settings resolve the path")
    context = retriever.gather("how does load_settings resolve the path", budget_tokens=800)
    assert "config_loader.py" in context and "load_settings" in context
