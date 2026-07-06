"""Core state layer: fs primitives, journal, plan graph, gates, artifacts, decisions."""

import threading

from engorc.artifacts import ArtifactStore, Handoff
from engorc.decisions import Decision, DecisionLog
from engorc.events import Journal, Kind
from engorc.fsio import FileLock, append_jsonl, atomic_write_json, iter_jsonl, read_json
from engorc.gates import GateBook
from engorc.plan import AttemptRecord, Plan, WorkItem, load_plan, save_plan
from engorc.util import human_age, iso_now, new_id, slugify, truncate_middle, truncate_tail


def test_ids_are_unique_and_time_ordered():
    ids = [new_id("x") for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(i.startswith("x_") for i in ids)
    # timestamp prefixes are non-decreasing (same-ms ties break on randomness)
    prefixes = [i[:11] for i in ids]
    assert prefixes == sorted(prefixes)


def test_text_shaping_helpers():
    assert slugify("Hello, World! Project #2") == "hello-world-project-2"
    long = "a" * 100 + "MIDDLE" + "b" * 100
    cut = truncate_middle(long, 60)
    assert len(cut) < len(long) and cut.startswith("a") and cut.endswith("b")
    tail = truncate_tail(long, 40)
    assert tail.endswith("b" * 10)
    assert human_age(iso_now()) in ("0s", "1s", "2s")


def test_atomic_json_and_jsonl(tmp_path):
    target = tmp_path / "deep" / "data.json"
    atomic_write_json(target, {"a": 1})
    assert read_json(target) == {"a": 1}
    log = tmp_path / "log.jsonl"
    for i in range(5):
        append_jsonl(log, {"i": i})
    assert [r["i"] for r in iter_jsonl(log)] == list(range(5))


def test_file_lock_excludes_concurrent_holders(tmp_path):
    lock_path = tmp_path / "locks" / "gpu.lock"
    order: list[str] = []

    def hold(name: str):
        lock = FileLock(lock_path, timeout=10)
        lock.acquire(label=name)
        order.append(f"{name}-in")
        threading.Event().wait(0.15)
        order.append(f"{name}-out")
        lock.release()

    threads = [threading.Thread(target=hold, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # whichever entered first must exit before the other enters
    first = order[0][0]
    assert order[1] == f"{first}-out"


def test_journal_appends_and_filters(tmp_path):
    journal = Journal(tmp_path / "journal")
    journal.append(Kind.PHASE_ENTERED, phase="charter")
    journal.append(Kind.AGENT_TURN, actor="implementer", item="wi_1", tool="run")
    journal.append(Kind.ERROR, error="boom")
    assert journal.count() == 3
    assert [e.kind for e in journal.iter_events(kinds=[Kind.ERROR])] == [Kind.ERROR]
    assert journal.tail(2)[-1].kind == Kind.ERROR
    assert journal.last(Kind.PHASE_ENTERED).payload["phase"] == "charter"


def _mini_plan() -> Plan:
    a = WorkItem(title="core")
    b = WorkItem(title="cli", depends_on=[a.id])
    return Plan(items=[a, b])


def test_plan_dag_validation_and_readiness(tmp_path):
    plan = _mini_plan()
    assert plan.validate_graph() == []
    ready = plan.ready_items()
    assert [i.title for i in ready] == ["core"]

    plan.items[0].status = "done"
    assert [i.title for i in plan.ready_items()] == ["cli"]

    plan.items[1].depends_on.append(plan.items[1].id)
    assert any("depends on itself" in p for p in plan.validate_graph())

    cyc_a = WorkItem(title="x")
    cyc_b = WorkItem(title="y", depends_on=[cyc_a.id])
    cyc_a.depends_on = [cyc_b.id]
    assert any("cycle" in p for p in Plan(items=[cyc_a, cyc_b]).validate_graph())

    path = tmp_path / "plan.yaml"
    save_plan(path, _mini_plan())
    loaded = load_plan(path)
    assert len(loaded.items) == 2 and loaded.items[1].depends_on == [loaded.items[0].id]


def test_dangling_attempt_cleanup():
    from engorc.orchestrator.supervisor import cleanup_dangling_attempts, pick_item

    plan = _mini_plan()
    item = plan.items[0]
    item.status = "in_progress"
    item.attempts.append(AttemptRecord(role="implementer"))
    assert cleanup_dangling_attempts(plan)
    assert item.status == "todo"
    assert item.attempts[0].outcome == "error"
    assert pick_item(plan, max_attempts=3).id == item.id
    item.attempts.extend([
        AttemptRecord(role="implementer", outcome="fail", ended=iso_now()),
        AttemptRecord(role="implementer", outcome="stuck", ended=iso_now()),
    ])
    # three failed attempts exhaust the budget, and the dependent item is not
    # ready while its dependency is unfinished — nothing is runnable
    assert pick_item(plan, max_attempts=3) is None


def test_gates_fold_answer_and_prefix(tmp_path):
    book = GateBook(tmp_path / "gates.jsonl")
    gate = book.open("Which database?", from_role="charterer", options=["sqlite", "postgres"])
    assert [g.id for g in book.open_gates()] == [gate.id]
    book.answer(gate.id[:8], "sqlite")
    assert book.open_gates() == []
    answered = book.answered_unconsumed(set())
    assert answered and answered[0].answer == "sqlite"
    assert book.answered_unconsumed({gate.id}) == []


def test_artifact_versioning(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write("design.md", "v1")
    store.write("design.md", "v2")
    store.write("design.md", "v3")
    assert store.read("design.md") == "v3"
    names = {p.name for p in store.list()}
    assert {"design.md", "design.v1.md", "design.v2.md"} <= names
    md = Handoff(from_role="implementer", summary="did things", warnings=["careful"]).to_markdown()
    assert "Handoff from implementer" in md and "careful" in md


def test_decision_log_supersede(tmp_path):
    log = DecisionLog(tmp_path / "decisions.jsonl")
    first = log.record(Decision(title="Use sqlite", decision="sqlite", confidence=0.9))
    log.supersede(first.id, Decision(title="Use postgres", decision="postgres", confidence=0.8))
    current = log.all()
    assert [d.title for d in current] == ["Use postgres"]
    assert len(log.all(include_superseded=True)) == 2
    assert "postgres" in log.render_markdown()
