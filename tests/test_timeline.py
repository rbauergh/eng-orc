"""GPU timeline: derives load/unload history with durations from state polls."""

import engorc.llm.timeline as timeline_module
from engorc.llm.timeline import GpuTimeline


class FakeClock:
    def __init__(self):
        self.now = "2026-07-06T10:00:00+00:00"

    def __call__(self) -> str:
        return self.now


def test_observe_derives_load_ready_unload_with_durations(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(timeline_module, "iso_now", clock)
    timeline = GpuTimeline(tmp_path)

    timeline.observe([{"model": "qwen3.6-35b", "state": "starting"}])
    clock.now = "2026-07-06T10:00:25+00:00"
    timeline.observe([{"model": "qwen3.6-35b", "state": "ready"}])
    clock.now = "2026-07-06T10:40:25+00:00"
    timeline.observe([{"model": "coder-fast", "state": "starting"}])  # swap happened

    events = timeline.recent(10)
    kinds = [(e["model"], e["event"]) for e in events]
    assert kinds == [
        ("qwen3.6-35b", "loading"),
        ("qwen3.6-35b", "ready"),
        ("qwen3.6-35b", "unloaded"),
        ("coder-fast", "loading"),
    ]
    ready = events[1]
    assert ready["load_seconds"] == 25.0
    unloaded = events[2]
    assert unloaded["resident_seconds"] == 2400.0

    current = timeline.current()
    assert current == [{"model": "coder-fast", "state": "loading", "for_seconds": 0.0}]

    described = [GpuTimeline.describe(e) for e in events]
    assert "loaded in 25s" in described[1]
    assert "unloaded after 40m00s" in described[2]


def test_normal_shutdown_is_unloaded_not_aborted(tmp_path, monkeypatch):
    """Regression: llama-swap reports a 'stopping' state during a normal
    unload; that must not be misread as a new load that then 'aborted'."""
    clock = FakeClock()
    monkeypatch.setattr(timeline_module, "iso_now", clock)
    timeline = GpuTimeline(tmp_path)

    timeline.observe([{"model": "glm", "state": "ready"}])
    clock.now = "2026-07-06T10:30:00+00:00"
    timeline.observe([{"model": "glm", "state": "stopping"}])
    clock.now = "2026-07-06T10:30:05+00:00"
    timeline.observe([])

    events = [(e["model"], e["event"]) for e in timeline.recent(10)]
    assert events == [("glm", "ready"), ("glm", "unloaded")]
    unloaded = timeline.recent(10)[-1]
    assert unloaded["resident_seconds"] == 1805.0  # measured from ready, through stopping


def test_slot_numbers_scavenges_nested_fields():
    from engorc.llm.server import SwapServer

    slot = {
        "id": 0,
        "is_processing": True,
        "next_token": {"n_decoded": 231, "has_next_token": True},
        "prompt_progress": {"n_past": 8123},
    }
    decoded, prompt = SwapServer._slot_numbers(slot)
    assert decoded == 231
    assert prompt == 8123


def test_observe_is_idempotent_when_nothing_changes(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(timeline_module, "iso_now", clock)
    timeline = GpuTimeline(tmp_path)
    timeline.observe([{"model": "m", "state": "ready"}])
    before = len(timeline.recent(10))
    clock.now = "2026-07-06T10:05:00+00:00"
    timeline.observe([{"model": "m", "state": "ready"}])
    assert len(timeline.recent(10)) == before
    assert timeline.current()[0]["for_seconds"] == 300.0
