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


def test_typical_load_seconds_is_median_of_real_loads(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(timeline_module, "iso_now", clock)
    timeline = GpuTimeline(tmp_path)
    assert timeline.typical_load_seconds("qwen") is None

    for load in (20, 30, 90):  # one outlier swap
        clock.now = "2026-07-06T10:00:00+00:00"
        timeline.observe([{"model": "qwen", "state": "starting"}])
        clock.now = f"2026-07-06T10:00:{load}+00:00" if load < 60 else "2026-07-06T10:01:30+00:00"
        timeline.observe([{"model": "qwen", "state": "ready"}])
        timeline.observe([])
    assert timeline.typical_load_seconds("qwen") == 30.0

    # first-observation "ready" records (load_seconds 0) are not loads
    timeline.observe([{"model": "warm", "state": "ready"}])
    assert timeline.typical_load_seconds("warm") is None


def _fake_services(tmp_path, running, activity=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        swap=SimpleNamespace(
            running_models=lambda: running,
            slot_activity=lambda model: activity,
        ),
        timeline=GpuTimeline(tmp_path),
    )


def test_gpu_wait_line_shows_load_progress_and_token_activity(tmp_path):
    from engorc.interactive import _gpu_wait_line

    # cold swap with history: a real progress bar against the typical time
    services = _fake_services(tmp_path, [{"model": "qwen", "state": "starting"}])
    for load_seconds in (40.0, 50.0, 60.0):
        timeline_module.append_jsonl(services.timeline.log_path, {
            "ts": "2026-07-06T09:00:00+00:00", "model": "qwen",
            "event": "ready", "load_seconds": load_seconds,
        })
    line = _gpu_wait_line(services, "drafting the spec", elapsed=25.0)
    assert "loading qwen" in line and "of ~50s typical" in line and "%" in line

    # cold swap without history: elapsed seconds, honestly labeled
    services = _fake_services(tmp_path / "fresh", [{"model": "glm", "state": "starting"}])
    line = _gpu_wait_line(services, "drafting the spec", elapsed=7.0)
    assert "loading glm" in line and "first load" in line

    # resident and generating: live token count
    services = _fake_services(tmp_path / "warm", [{"model": "qwen", "state": "ready"}],
                              activity=(1, 132, 900))
    line = _gpu_wait_line(services, "drafting the spec", elapsed=9.0)
    assert "generating (132 tokens)" in line

    # server unreachable: plain elapsed, never a crash
    from types import SimpleNamespace
    broken = SimpleNamespace(swap=SimpleNamespace(running_models=lambda: 1 / 0),
                             timeline=None)
    assert _gpu_wait_line(broken, "drafting the spec", elapsed=3.0) == "drafting the spec … 3s"


def test_call_with_progress_passthrough_off_terminal():
    from engorc.interactive import call_with_progress

    assert call_with_progress(None, lambda: 42) == 42
    import pytest

    with pytest.raises(ValueError):
        call_with_progress(None, lambda: (_ for _ in ()).throw(ValueError("boom")))


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
