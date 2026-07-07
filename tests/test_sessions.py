"""Interactive session presence: intake advertises itself so the dashboard
explains the busy GPU and the scheduler yields instead of swapping the
conversation's model out mid-turn."""

import os

import engorc.sessions as sessions_module
from engorc.fsio import atomic_write_json
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.scheduler import Scheduler
from engorc.orchestrator.services import Services
from engorc.registry import Registry
from engorc.sessions import active_sessions, foreign_sessions, interactive_session


def test_session_file_lives_exactly_as_long_as_the_conversation(tmp_path):
    with interactive_session(tmp_path, "intake", "orc new -i") as session:
        (record,) = active_sessions(tmp_path)
        assert record["kind"] == "intake" and record["detail"] == "orc new -i"
        assert record["pid"] == os.getpid()
        session.update("waiting for your answer at the prompt")
        (record,) = active_sessions(tmp_path)
        assert record["status"] == "waiting for your answer at the prompt"
        # my own session is not "foreign" — a process never yields to itself
        assert foreign_sessions(tmp_path) == []
    assert active_sessions(tmp_path) == []


def test_dead_process_sessions_are_cleaned_up(tmp_path, monkeypatch):
    stale = tmp_path / "sessions" / "intake-99999.json"
    stale.parent.mkdir(parents=True)
    atomic_write_json(stale, {"kind": "intake", "pid": 99999, "status": "zombie"})
    monkeypatch.setattr(sessions_module, "_pid_alive", lambda pid: False)
    assert active_sessions(tmp_path) == []
    assert not stale.exists()
    assert not sessions_module._pid_alive(-1)  # never signal pid<=0


def test_scheduler_yields_to_a_foreign_interactive_session(config, monkeypatch):
    services = Services.build(config, client=FakeLLM(lambda *a: "must not be called"))
    Registry(config).create("ready mission", title="R")  # runnable, would step

    session_file = config.home / "sessions" / "intake-99999.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(session_file, {"kind": "intake", "detail": "orc new -i",
                                     "pid": 99999, "status": "drafting"})
    monkeypatch.setattr(sessions_module, "_pid_alive", lambda pid: True)

    scheduler = Scheduler(services)
    assert scheduler.step() is None
    assert scheduler.run(watch=False) == 0
    assert not services.client.calls

    session_file.unlink()  # conversation over — the same scheduler steps again
    assert scheduler.step() is not None


def test_dashboard_surfaces_the_interactive_session(config, monkeypatch):
    from engorc.obs.dashboard import _now_panel, gather_snapshot

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    session_file = config.home / "sessions" / "intake-99999.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(session_file, {
        "kind": "intake", "detail": "orc new -i", "pid": 99999,
        "started": "2026-07-07T10:00:00+00:00",
        "status": "the model is drafting (round 2/10)",
    })
    monkeypatch.setattr(sessions_module, "_pid_alive", lambda pid: True)

    snapshot = gather_snapshot(services)
    assert snapshot.sessions and snapshot.sessions[0]["kind"] == "intake"
    _, panel = _now_panel(snapshot)
    rendered = panel.renderable.plain
    assert "intake" in rendered and "drafting (round 2/10)" in rendered
    assert "yielding" in rendered
