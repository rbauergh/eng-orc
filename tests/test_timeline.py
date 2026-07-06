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
