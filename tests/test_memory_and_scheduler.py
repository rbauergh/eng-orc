"""Memory stores, recall, and scheduler policy."""

from engorc.memory.local_store import LocalMemoryStore
from engorc.memory.recall import build_recall_section, recall_markdown
from engorc.memory.schema import MemoryItem
from engorc.memory.store import CompositeMemory, open_memory
from engorc.orchestrator.scheduler import Scheduler
from engorc.orchestrator.services import Services
from engorc.registry import Registry


def test_local_store_search_blocks_and_outbox(tmp_path):
    store = LocalMemoryStore(tmp_path / "memory.db")
    store.save(MemoryItem(kind="lesson", title="WSL mnt is slow",
                          body="Keep models on ext4; /mnt/c kills mmap performance", project="p1"))
    store.save(MemoryItem(kind="convention", title="pytest flags",
                          body="Use -q --color=no in automation"))
    hits = store.search("ext4 mmap performance models")
    assert hits and hits[0].item.title == "WSL mnt is slow"
    assert store.search("ext4", kinds=["convention"]) == [] or all(
        h.item.kind == "convention" for h in store.search("ext4", kinds=["convention"])
    )
    store.set_block("user_profile", "prefers Makefiles")
    assert store.get_block("user_profile") == "prefers Makefiles"
    item = MemoryItem(kind="note", title="queued", body="offline write")
    store.save(item)
    store.outbox_add(item.id)
    assert [i.id for i in store.outbox_items()] == [item.id]
    store.outbox_remove(item.id)
    assert store.outbox_items() == []


def test_composite_falls_back_when_letta_absent(tmp_path):
    memory = CompositeMemory(LocalMemoryStore(tmp_path / "m.db"), letta=None)
    memory.save(MemoryItem(kind="lesson", title="t", body="always verify before done"))
    assert memory.search("verify before done")
    ok, detail = memory.health()
    assert ok and "letta: not configured" in detail
    assert memory.sync() == 0


def test_recall_sections_render_and_never_raise(tmp_path, config):
    memory = open_memory(config)
    memory.save(MemoryItem(kind="lesson", title="Lock the GPU", body="One model at a time."))
    section = build_recall_section(memory, "gpu lock model")
    assert "Lock the GPU" in section
    assert recall_markdown([]) == ""


def _quiet_services(config):
    from engorc.llm.fake import FakeLLM

    return Services.build(config, client=FakeLLM(lambda *a: "unused"))


def test_scheduler_ordering_and_parking(config):
    services = _quiet_services(config)
    registry = Registry(config)
    low = registry.create("later thing", title="Low", priority=4)
    high = registry.create("urgent thing", title="High", priority=1)
    parked = registry.create("blocked thing", title="Parked", priority=1)
    parked.set_state("blocked_on_user", reason="waiting")
    paused = registry.create("paused thing", title="Paused")
    paused.set_state("paused", reason="user said so")

    scheduler = Scheduler(services)
    order = [p.root.name for p in scheduler.runnable()]
    assert order[0] == high.root.name
    assert low.root.name in order
    assert parked.root.name not in order
    assert paused.root.name not in order

    # an answered gate revives a parked project on the next pass
    gate = parked.gates.open("pick one", from_role="charterer")
    parked.gates.answer(gate.id, "option a")
    revived = [p.root.name for p in scheduler.runnable()]
    assert parked.root.name in revived
    assert services.registry.get(parked.root.name).meta.state == "active"


def test_step_on_unrunnable_project_returns_none(config):
    services = _quiet_services(config)
    registry = Registry(config)
    project = registry.create("something", title="Thing")
    project.set_state("paused", reason="nope")
    assert Scheduler(services).step(project.root.name) is None
