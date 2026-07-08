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


def test_ensure_repo_seeds_gitignore_and_untracks_junk(tmp_path):
    """Regression: no .gitignore meant `git add -A` committed the project
    venv; a reviewer flagged '.venv in the repository' as an ARCHITECTURE
    blocker. Existing repos get the junk untracked in a standalone commit."""
    from engorc.agents.toolbox.git import _git, commit_all, ensure_repo

    workroom = tmp_path / "workroom"
    workroom.mkdir()
    ensure_repo(workroom)
    assert (workroom / ".gitignore").exists()
    (workroom / ".venv" / "lib").mkdir(parents=True)
    (workroom / ".venv" / "lib" / "x.py").write_text("junk\n")
    (workroom / "app.py").write_text("real\n")
    commit_all(workroom, "feat: app")
    _, tracked = _git(workroom, "ls-files")
    assert ".venv/lib/x.py" not in tracked and "app.py" in tracked

    # a pre-existing repo with a committed venv gets migrated on first touch
    legacy = tmp_path / "legacy"
    (legacy / ".venv").mkdir(parents=True)
    (legacy / ".venv" / "big.bin").write_text("x" * 10)
    (legacy / "main.py").write_text("code\n")
    _git(legacy, "init", "-b", "main")
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        _git(legacy, "config", key, value)
    _git(legacy, "add", "-A")
    _git(legacy, "commit", "-m", "old state with venv committed")
    ensure_repo(legacy)
    _, tracked = _git(legacy, "ls-files")
    assert ".venv/big.bin" not in tracked and "main.py" in tracked
    assert (legacy / ".venv" / "big.bin").exists()  # untracked, not deleted


def test_attempt_diffs_exclude_other_items_leftovers(config):
    """Regression: reviewers judged one item's diff against other items'
    uncommitted debris ('scope creep', 'missing files'). A checkpoint commit
    before each attempt makes the diff exactly the attempt's own work."""
    from engorc.agents.toolbox.git import _git, commit_all, ensure_repo
    from engorc.config import Config, IndexConfig, MemoryConfig, RunConfig
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.phases import phase_build
    from engorc.orchestrator.services import Services
    from engorc.plan import Plan, WorkItem
    from engorc.registry import Registry

    cfg = Config(home=config.home, memory=MemoryConfig(backend="local"),
                 index=IndexConfig(enabled=False),
                 run=RunConfig(project_venvs=False, review_required=False))
    project = Registry(cfg).create("isolation mission", title="I")
    ensure_repo(project.workroom)
    (project.workroom / "base.py").write_text("base\n")
    commit_all(project.workroom, "feat: base")
    # debris from another item's failed attempt, uncommitted on purpose
    (project.workroom / "leftover.py").write_text("half-finished other item\n")

    item = WorkItem(title="write b", verify_commands=["test -f b.py"])
    project.save_plan(Plan(items=[item]))

    replies = iter([
        'writing\n\nACTION: write_file {"path": "b.py"}\n```python\nprint("b")\n```\n',
        'done\n\nACTION: finish {"status": "done"}\n```payload\nwrote b.py\n```\n',
    ])
    services = Services.build(cfg, client=FakeLLM(lambda *a: next(replies)))
    note = phase_build(services, project)
    assert "completed" in note

    log_out = _git(project.workroom, "log", "--oneline")[1]
    assert "checkpoint: carry-over" in log_out
    # the item's integration commit contains ONLY its own file
    _, integrated = _git(project.workroom, "show", "--name-only", "--format=", "HEAD")
    assert "b.py" in integrated and "leftover.py" not in integrated


def test_tool_loop_grows_budget_when_thinking_eats_it(ctx, config):
    """Regression: gpt-oss burned its whole output budget reasoning and
    produced empty replies — three identical FORMAT ERRORs with the same
    budget each time. A length-finish now grows the budget and says so."""
    from engorc.agents.runtime import ToolLoop
    from engorc.agents.toolbox import ALL_TOOLS
    from engorc.config import RoleModel
    from engorc.events import Journal
    from engorc.llm.fake import FakeLLM

    finish = 'done\n\nACTION: finish {"status": "done"}\n```payload\nall good\n```\n'
    replies = iter([("", "", "length"), finish])
    client = FakeLLM(lambda *a: next(replies))
    loop = ToolLoop(
        client=client, config=config, role_name="implementer",
        role_model=RoleModel(model="m", max_output_tokens=1000),
        tools=[ALL_TOOLS["finish"]], ctx=ctx,
        journal=Journal(ctx.project.root / "journal"),
        system_prompt="# Implementer",
    )
    result = loop.run("brief", "task", max_turns=5)
    assert result.status == "done"
    assert client.calls[0]["max_tokens"] == 1000
    assert client.calls[1]["max_tokens"] == 1500


def _loop(ctx, config, client, tool_names=("run", "write_file", "finish"), max_output=1000):
    from engorc.agents.runtime import ToolLoop
    from engorc.agents.toolbox import ALL_TOOLS
    from engorc.config import RoleModel
    from engorc.events import Journal

    return ToolLoop(
        client=client, config=config, role_name="tester",
        role_model=RoleModel(model="m", max_output_tokens=max_output),
        tools=[ALL_TOOLS[n] for n in tool_names], ctx=ctx,
        journal=Journal(ctx.project.root / "journal"),
        system_prompt="# Tester",
    )


def test_payload_in_json_args_is_salvaged_with_a_nudge(ctx, config):
    """Regression: the tester spent 50 turns repeating
    ACTION: run {"payload": "..."} against 'run needs the shell command in the
    fenced payload'. The string already survived JSON parsing — accept it and
    teach the fence instead of burning the attempt."""
    from engorc.llm.fake import FakeLLM

    seen: list[list[dict]] = []
    replies = iter([
        'testing\n\nACTION: run {"payload": "echo salvaged-ok"}\n',
        'writing\n\nACTION: write_file {"path": "t.py", "content": "print(1)"}\n',
        'done\n\nACTION: finish {"status": "done"}\n```payload\nall good\n```\n',
    ])

    def brain(messages, response_format, role_model):
        seen.append(messages)
        return next(replies)

    result = _loop(ctx, config, FakeLLM(brain)).run("brief", "task", max_turns=5)
    assert result.status == "done"
    assert (ctx.workroom / "t.py").read_text().strip() == "print(1)"
    # the run executed AND the model was taught the fence
    second_call = seen[1][-1]["content"]
    assert "salvaged-ok" in second_call
    assert "fenced" in second_call and "Accepted this time" in second_call


def test_truncated_reply_missing_payload_grows_budget(ctx, config):
    """Regression: the output budget died right after the ACTION line, the
    payload fence never appeared, and the tool's emptiness error repeated
    until stuck — the format-error budget growth never fired because the
    ACTION line itself parsed fine."""
    from engorc.llm.fake import FakeLLM

    replies = iter([
        ('plan\n\nACTION: write_file {"path": "a.py"}\n', "", "length"),
        'retry\n\nACTION: write_file {"path": "a.py"}\n```payload\nprint("a")\n```\n',
        'done\n\nACTION: finish {"status": "done"}\n```payload\nwrote a.py\n```\n',
    ])
    client = FakeLLM(lambda *a: next(replies))
    result = _loop(ctx, config, client).run("brief", "task", max_turns=5)
    assert result.status == "done"
    assert (ctx.workroom / "a.py").exists()
    assert client.calls[0]["max_tokens"] == 1000
    assert client.calls[1]["max_tokens"] == 1500


def test_edit_file_error_teaches_the_full_action_shape(ctx):
    from engorc.agents.toolbox import ALL_TOOLS

    (ctx.workroom / "f.py").write_text("x = 1\n")
    result = ALL_TOOLS["edit_file"].run(ctx, {"path": "f.py"}, "not a block")
    assert not result.ok
    assert "ACTION: edit_file" in result.output
    assert "<<<<<<< SEARCH" in result.output and "```payload" in result.output


def test_compaction_triggers_on_context_pressure_not_turn_count(ctx, config):
    """Compaction fires when the prompt actually nears the model's window
    (the server's own token count), not at an arbitrary turn number."""
    from engorc.agents.runtime import ToolLoop
    from engorc.agents.toolbox import ALL_TOOLS
    from engorc.config import RoleModel
    from engorc.events import Journal
    from engorc.llm.fake import FakeLLM

    (ctx.workroom / "big.txt").write_text(
        "\n".join(f"line {i}: " + "content " * 12 for i in range(600)))
    config.run.compact_after_turns = 999  # the turn trigger is out of play
    openings: list[str] = []
    turn = {"n": 0}

    def brain(messages, response_format, role_model):
        if messages[0]["content"].startswith("You compress"):
            return "compacted summary of the exploration"
        if len(messages) > 1:
            openings.append(messages[1]["content"])
        turn["n"] += 1
        if turn["n"] >= 9:
            return 'done\n\nACTION: finish {"status": "done"}\n```payload\nok\n```\n'
        return f'reading\n\nACTION: read_file {{"path": "big.txt", "start": {turn["n"] * 7}}}\n'

    loop = ToolLoop(
        client=FakeLLM(brain), config=config, role_name="scout",
        role_model=RoleModel(model="m", context_window=4000, max_output_tokens=200),
        tools=[ALL_TOOLS["read_file"], ALL_TOOLS["finish"]], ctx=ctx,
        journal=Journal(ctx.project.root / "journal"),
        system_prompt="# Scout",
    )
    result = loop.run("brief", "task", max_turns=12)
    assert result.status == "done"
    assert any("compacted" in opening for opening in openings)


def test_stall_detection_cuts_an_unproductive_loop_early(ctx, config):
    """Regression request: 'failure detection should be more dynamic than a
    hard-coded turn count.' An agent producing no file changes and no new
    command results is cut at stall_turns, long before the base cap — after
    being warned."""
    from engorc.llm.fake import FakeLLM

    config.run.stall_turns = 4
    seen: list[str] = []
    flip = {"n": 0}

    def brain(messages, response_format, role_model):
        seen.append(messages[-1]["content"])
        flip["n"] += 1  # alternate two reads so the identical-action detector stays quiet
        path = "a.txt" if flip["n"] % 2 else "b.txt"
        return f'looking\n\nACTION: read_file {{"path": "{path}"}}\n'

    (ctx.workroom / "a.txt").write_text("a\n")
    (ctx.workroom / "b.txt").write_text("b\n")
    result = _loop(ctx, config, FakeLLM(brain),
                   tool_names=("read_file", "write_file", "run", "finish")).run(
        "brief", "task", max_turns=40)
    assert result.status == "stuck"
    assert "stalled: no file changes or new command results" in result.summary
    assert result.turns == 4  # not 40
    assert any("you appear stuck" in content for content in seen)  # warned first


def test_progress_earns_turns_past_the_base_cap(ctx, config):
    """The inverse: steady progress extends the budget to the hard ceiling,
    and the final summary tells triage the item is too big — not that the
    agent failed."""
    from engorc.llm.fake import FakeLLM

    config.run.stall_turns = 4
    counter = {"n": 0}

    def brain(messages, response_format, role_model):
        counter["n"] += 1
        return (f'building\n\nACTION: write_file {{"path": "part{counter["n"]}.py"}}\n'
                f"```payload\nprint({counter['n']})\n```\n")

    result = _loop(ctx, config, FakeLLM(brain)).run("brief", "task", max_turns=3)
    assert result.status == "stuck"
    assert result.turns == 6  # 2x the base cap: progress earned the extension
    assert "still making progress" in result.summary and "too big" in result.summary


def test_stuck_summary_carries_the_proximate_cause(ctx, config):
    from engorc.llm.fake import FakeLLM

    fail = 'try\n\nACTION: run {}\n```payload\nfalse\n```\n'
    result = _loop(ctx, config, FakeLLM(lambda *a: fail)).run("brief", "task", max_turns=2)
    assert result.status == "stuck"
    assert "last observation:" in result.summary
    assert "exit code 1" in result.summary


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
