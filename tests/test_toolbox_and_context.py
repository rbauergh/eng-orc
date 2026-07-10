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


def test_role_boundary_keeps_the_tester_out_of_source(ctx):
    """Regression: a tester on 'add error handling to main.py' edited main.py
    itself — implementing the item to green its own tests, leaving the
    implementer an empty diff and bypassing the full review panel (tester
    diffs only get the tests-lens review)."""
    from engorc.agents.toolbox.fs import is_test_path

    ctx.role = "tester"
    result = tool("write_file").run(ctx, {"path": "connect4/main.py"}, "x = 1")
    assert not result.ok and "role boundary" in result.output
    assert "finish with a" in result.output  # the refusal names the escape hatch
    assert not (ctx.workroom / "connect4/main.py").exists()

    for allowed in ("test_main.py", "tests/fixtures/conftest.py", "ui/app.spec.ts"):
        result = tool("write_file").run(ctx, {"path": allowed}, "assert True")
        assert result.ok, allowed

    assert not is_test_path("contest.py")  # near-miss names stay source


def test_role_boundary_keeps_the_implementer_out_of_a_delivered_suite(ctx):
    from engorc.agents.toolbox.fs import DIVIDER_MARK, REPLACE_MARK, SEARCH_MARK

    (ctx.workroom / "test_main.py").write_text("def test_x():\n    assert False\n")
    payload = f"{SEARCH_MARK}\nassert False\n{DIVIDER_MARK}\nassert True\n{REPLACE_MARK}"

    ctx.role = "implementer"  # no tester on this item: tests are theirs to write
    assert tool("edit_file").run(ctx, {"path": "test_main.py"}, payload).ok

    ctx.extras["suite_owned"] = True
    result = tool("edit_file").run(ctx, {"path": "test_main.py"}, payload)
    assert not result.ok and "contract" in result.output
    result = tool("write_file").run(ctx, {"path": "main.py"}, "x = 1")
    assert result.ok  # source stays fully theirs


def test_silent_success_says_so(ctx):
    """Regression: a find over a nonexistent path exits 0 with nothing — the
    bare 'exit code 0' observation taught the model nothing and it retried
    the same probe until the repeat detector killed the attempt."""
    from engorc.agents.toolbox.shell import run_command

    result = run_command(ctx, "true", timeout=10)
    assert result.ok
    assert "no output — the command succeeded but printed nothing" in result.output


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
        # the exact shape that repeated three times in the wild: a perfect
        # handoff, parked in an args key named after the tool's own docs
        'done\n\nACTION: finish {"status": "done", "handoff": "all good, no changes needed"}\n',
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


def test_batched_actions_execute_the_first_and_say_so(ctx, config):
    """Regression: three ACTION lines in one reply were rejected wholesale,
    discarding a complete write_file payload; the model then spent the rest
    of the attempt hunting for a file it believed it had written."""
    from engorc.llm.fake import FakeLLM

    seen: list[list[dict]] = []
    replies = iter([
        'batch\n\nACTION: write_file {"path": "notes.txt"}\n```\nhello\n```\n'
        'ACTION: run {"timeout": 60}\n```bash\ncat notes.txt\n```\n',
        'done\n\nACTION: finish {"status": "done"}\n```payload\nok\n```\n',
    ])

    def brain(messages, response_format, role_model):
        seen.append(messages)
        return next(replies)

    result = _loop(ctx, config, FakeLLM(brain)).run("brief", "task", max_turns=5)
    assert result.status == "done"
    assert (ctx.workroom / "notes.txt").read_text().strip() == "hello"
    second_call = seen[1][-1]["content"]
    assert "only the FIRST" in second_call and "NOT run: run" in second_call


def test_orphan_holding_the_pipe_does_not_stall_the_capture(ctx):
    """Regression: a verify command launched the built game and exited in
    seconds, but the detached process inherited stdout — the capture blocked
    for the full 600s tool timeout, and the orphan kept running afterward.
    The group kill guarantees the readers reach EOF (an EOF is only possible
    once every inherited writer is dead)."""
    import time

    from engorc.agents.toolbox.shell import run_command

    start = time.monotonic()
    result = run_command(ctx, "echo started; sleep 300 &", timeout=60)
    elapsed = time.monotonic() - start
    assert result.ok and "started" in result.output
    assert elapsed < 30  # the ~2s grace + group kill, not the 60s timeout


def test_timeout_kills_the_group_and_keeps_partial_output(ctx):
    import time

    from engorc.agents.toolbox.shell import run_command

    start = time.monotonic()
    result = run_command(ctx, "echo progress; sleep 60", timeout=1)
    elapsed = time.monotonic() - start
    assert not result.ok and result.data.get("timed_out") is True
    assert "timed out after 1s" in result.output
    assert "progress" in result.output  # evidence survives the timeout
    assert elapsed < 20


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
    from engorc.agents.runtime import FORMAT_CONTRACT
    from engorc.agents.toolbox import ALL_TOOLS

    (ctx.workroom / "f.py").write_text("x = 1\n")
    result = ALL_TOOLS["edit_file"].run(ctx, {"path": "f.py"}, "not a block")
    assert not result.ok
    assert "ACTION: edit_file" in result.output
    assert "<<<<<<< SEARCH" in result.output and "```payload" in result.output
    # the observed dead loop was a model that wanted to APPEND: give it the
    # tool it already uses successfully
    assert "use write_file with the COMPLETE new file contents" in result.output
    # and the format contract itself carries one fully-worked payload example
    assert "<<<<<<< SEARCH" in FORMAT_CONTRACT
    assert "SAME reply" in FORMAT_CONTRACT


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


def test_final_turns_recitation_demands_convergence():
    """Regression: a scout consumed all 32 earned turns still reading and died
    with nothing written — the deadline must convert into 'write the report
    NOW', not a silent guillotine."""
    from engorc.agents.runtime import ToolLoop

    calm = ToolLoop._recitation("task", turns_left=10, touched=[])
    assert "FINAL TURNS" not in calm
    urgent = ToolLoop._recitation("task", turns_left=2, touched=[])
    assert "FINAL TURNS" in urgent and "finish NOW" in urgent


def test_scout_novelty_earns_turns_past_the_base_cap(ctx, config):
    """Regression: read-only roles were exempt from earned extension — a scout
    productively reading NEW ground died at its base cap mid-investigation.
    New information (a successful never-executed action) now extends it."""
    from engorc.llm.fake import FakeLLM

    (ctx.workroom / "big.txt").write_text("\n".join(f"l{i}" for i in range(400)))
    turn = {"n": 0}

    def brain(messages, response_format, role_model):
        turn["n"] += 1
        if turn["n"] >= 6:
            return 'done\n\nACTION: finish {"status": "done"}\n```payload\nreport\n```\n'
        return f'reading\n\nACTION: read_file {{"path": "big.txt", "start": {turn["n"] * 50}}}\n'

    result = _loop(ctx, config, FakeLLM(brain),
                   tool_names=("read_file", "finish")).run("brief", "task", max_turns=3)
    assert result.status == "done"
    assert result.turns == 6  # past the base cap of 3, earned by new ground


def test_scout_repeating_ground_stalls_out(ctx, config):
    from engorc.llm.fake import FakeLLM

    config.run.stall_turns = 4
    (ctx.workroom / "a.txt").write_text("a\n")
    (ctx.workroom / "b.txt").write_text("b\n")
    flip = {"n": 0}

    def brain(messages, response_format, role_model):
        flip["n"] += 1  # alternate the SAME two reads: nothing new after turn 2
        return f'looking\n\nACTION: read_file {{"path": "{"a" if flip["n"] % 2 else "b"}.txt"}}\n'

    result = _loop(ctx, config, FakeLLM(brain),
                   tool_names=("read_file", "finish")).run("brief", "task", max_turns=40)
    assert result.status == "stuck"
    assert "no new information" in result.summary
    assert result.turns == 6  # last new ground at turn 2 + stall window 4


def test_stall_detection_cuts_an_unproductive_loop_early(ctx, config):
    """Regression request: 'failure detection should be more dynamic than a
    hard-coded turn count.' An agent covering no new ground is cut at
    stall_turns — after being warned — and its knowledge is salvaged for
    the next attempt instead of dying with the transcript."""
    from engorc.llm.fake import FakeLLM

    config.run.stall_turns = 4
    seen: list[str] = []
    flip = {"n": 0}

    def brain(messages, response_format, role_model):
        if messages[0]["content"].startswith("You compress"):
            return "salvage: read a.txt and b.txt; both trivial; nothing written yet"
        seen.append(messages[-1]["content"])
        flip["n"] += 1  # alternate the SAME two reads: novel only twice
        path = "a.txt" if flip["n"] % 2 else "b.txt"
        return f'looking\n\nACTION: read_file {{"path": "{path}"}}\n'

    (ctx.workroom / "a.txt").write_text("a\n")
    (ctx.workroom / "b.txt").write_text("b\n")
    result = _loop(ctx, config, FakeLLM(brain),
                   tool_names=("read_file", "write_file", "run", "finish")).run(
        "brief", "task", max_turns=40)
    assert result.status == "stuck"
    assert "stalled: no new information" in result.summary
    assert result.turns == 6  # 2 novel reads + the 4-turn stall window; not 40
    assert any("you appear stuck" in content for content in seen)  # warned first
    assert "salvage: read a.txt and b.txt" in result.handoff_md  # knowledge kept


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


def test_vector_chunks_serve_live_files_never_the_stale_store(tmp_path, config):
    """ROOT CAUSE of 'SEARCH text not found': the semantic index stored chunk
    TEXT at index time, and the index only refreshes at item integration —
    briefs were quoting file versions that no longer existed. The index now
    only RANKS; content is re-read from disk, and vanished chunks are dropped."""
    from types import SimpleNamespace

    from engorc.context.indexer import Snippet
    from engorc.context.repomap import RepoMap
    from engorc.context.retriever import HybridRetriever

    workroom = tmp_path / "wr"
    workroom.mkdir()
    (workroom / "f.py").write_text("def hover_col(x):\n    return (x - 10) // 40\n")
    stale = Snippet(path="f.py", text="def hover_col(x):\n    return x // 32\n", score=0.9)
    retriever = HybridRetriever(
        workroom=workroom, config=config,
        repomap=RepoMap(workroom=workroom, cache_dir=tmp_path / "cache",
                        ignore=[], max_kb=256),
        index=SimpleNamespace(search=lambda q, top_k=None: [stale]),
    )
    out = retriever.gather("hover_col mapping")
    assert "(x - 10) // 40" in out  # what the file says NOW
    assert "x // 32" not in out     # what the store remembered

    (workroom / "f.py").write_text("everything_rewritten = True\n")
    out = retriever.gather("hover_col mapping")
    assert "x // 32" not in out  # anchor gone → stale chunk dropped entirely


def test_edit_file_tolerates_model_drift(ctx):
    """Regression: 'SEARCH text not found' — models drift from the real file
    (copied line-number prefixes, trailing whitespace, indentation) and then
    stall. Unambiguous near-matches now apply; true misses show the closest
    REAL lines to copy."""
    from engorc.agents.toolbox import ALL_TOOLS

    (ctx.workroom / "m.py").write_text(
        "def top():\n    # the hover rect anchors col 5\n    return 5   \n")

    # copied line-number prefixes from read_file output
    payload = ("<<<<<<< SEARCH\n2|     # the hover rect anchors col 5\n"
               "3|     return 5   \n=======\n    return 6\n>>>>>>> REPLACE")
    result = ALL_TOOLS["edit_file"].run(ctx, {"path": "m.py"}, payload)
    assert result.ok, result.output
    assert "return 6" in (ctx.workroom / "m.py").read_text()

    # trailing-whitespace drift (the model added spaces the file lacks)
    (ctx.workroom / "w.py").write_text("def f():\n    return 1\n")
    payload = "<<<<<<< SEARCH\n    return 1   \n=======\n    return 2\n>>>>>>> REPLACE"
    result = ALL_TOOLS["edit_file"].run(ctx, {"path": "w.py"}, payload)
    assert result.ok and "trailing whitespace" in result.output
    assert "return 2" in (ctx.workroom / "w.py").read_text()

    # indentation drift is the MODEL's error, not tolerated silently — but
    # the miss teaches with the closest REAL region, numbered and copyable
    (ctx.workroom / "i.py").write_text("class C:\n    def g(self):\n        return 1\n")
    payload = ("<<<<<<< SEARCH\ndef g(self):\n    return 1\n=======\n"
               "def g(self):\n    return 2\n>>>>>>> REPLACE")
    result = ALL_TOOLS["edit_file"].run(ctx, {"path": "i.py"}, payload)
    assert not result.ok
    assert "Closest existing text" in result.output
    assert "def g(self):" in result.output  # actual file content, copyable


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
