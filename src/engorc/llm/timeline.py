"""GPU model timeline: who was resident when, and what swaps cost.

llama-swap only reports the CURRENT process states (/running), so orc derives
the history itself: every observer (scheduler step boundaries, review-panel
seats, the dashboard's refresh tick, `orc models`) feeds the same recorder,
which diffs against the last known state under a file lock and appends
transitions to ~/.eng-orc/gpu-timeline.jsonl:

    {ts, model, event: "loading"}
    {ts, model, event: "ready",    load_seconds}
    {ts, model, event: "unloaded", resident_seconds}
    {ts, model, event: "aborted",  loading_seconds}   (died before ready)

Durations are as-observed: polling granularity bounds their precision, which
is plenty for "the 35B takes ~25s to load and sat resident for 40 minutes".
"""

from __future__ import annotations

from pathlib import Path

from ..fsio import append_jsonl, atomic_write_json, flocked, iter_jsonl, read_json
from ..util import human_duration, iso_now, parse_iso, progress_bar

_READY_STATES = ("ready", "running", "loaded")
_STOPPING_STATES = ("stopping", "stopped", "shutdown", "shuttingdown", "stop")


def normalize_states(running: list[dict]) -> dict[str, str]:
    """Collapse llama-swap's state vocabulary to ready/stopping/loading."""
    states: dict[str, str] = {}
    for entry in running:
        model = entry.get("model")
        if not model:
            continue
        raw = (entry.get("state") or "").lower().replace("_", "").replace("-", "")
        if raw in _READY_STATES or raw == "":
            states[model] = "ready"
        elif raw in _STOPPING_STATES:
            states[model] = "stopping"
        else:
            states[model] = "loading"
    return states


def _seconds_between(start_ts: str, end_ts: str) -> float:
    try:
        return max(0.0, (parse_iso(end_ts) - parse_iso(start_ts)).total_seconds())
    except ValueError:
        return 0.0


class GpuTimeline:
    def __init__(self, home: Path):
        self.state_path = home / "gpu-state.json"
        self.log_path = home / "gpu-timeline.jsonl"

    def observe(self, running: list[dict]) -> None:
        """Diff current server state against the last observation; append any
        transitions. Safe under concurrent observers (dashboard + scheduler)."""
        now = iso_now()
        states = normalize_states(running)
        with flocked(self.state_path):
            previous: dict = read_json(self.state_path, default={})
            changed = False
            current: dict = {}
            # departures first: an unload precedes its successor's load
            for model, prior in previous.items():
                if model in states:
                    continue
                event = "unloaded" if prior["state"] == "ready" else "aborted"
                duration_key = "resident_seconds" if event == "unloaded" else "loading_seconds"
                append_jsonl(self.log_path, {
                    "ts": now, "model": model, "event": event,
                    duration_key: round(_seconds_between(prior["since"], now), 1),
                })
                changed = True
            for model, state in states.items():
                prior = previous.get(model)
                if state == "stopping":
                    # a normal shutdown in progress is NOT a new load: keep the
                    # prior record so the eventual disappearance is accounted
                    # as unloaded-with-residency (not a bogus "aborted")
                    current[model] = prior or {"state": "ready", "since": now}
                    continue
                if prior is None:
                    append_jsonl(self.log_path, {"ts": now, "model": model, "event": "loading"}
                                 if state == "loading"
                                 else {"ts": now, "model": model, "event": "ready", "load_seconds": 0.0})
                    current[model] = {"state": state, "since": now}
                    changed = True
                elif prior["state"] != state:
                    if state == "ready":
                        append_jsonl(self.log_path, {
                            "ts": now, "model": model, "event": "ready",
                            "load_seconds": round(_seconds_between(prior["since"], now), 1),
                        })
                    else:
                        append_jsonl(self.log_path, {"ts": now, "model": model, "event": "loading"})
                    current[model] = {"state": state, "since": now}
                    changed = True
                else:
                    current[model] = prior
            if changed or not self.state_path.exists():
                atomic_write_json(self.state_path, current)

    # -- reading ---------------------------------------------------------------
    def current(self) -> list[dict]:
        """Resident/loading models with how long they have been in that state."""
        now = iso_now()
        out = []
        for model, info in (read_json(self.state_path, default={}) or {}).items():
            out.append({
                "model": model,
                "state": info.get("state", "?"),
                "for_seconds": _seconds_between(info.get("since", now), now),
            })
        return sorted(out, key=lambda entry: entry["model"])

    def recent(self, n: int = 8) -> list[dict]:
        events = list(iter_jsonl(self.log_path))
        return events[-n:]

    def typical_load_seconds(self, model: str, window: int = 5) -> float | None:
        """Median of the model's recent observed load times — the basis for
        'loading X, about 30s to go'. None until a real load has been seen
        (sub-second 'ready' records are first-observations, not loads)."""
        loads = [
            event.get("load_seconds", 0.0)
            for event in iter_jsonl(self.log_path)
            if event.get("model") == model and event.get("event") == "ready"
            and (event.get("load_seconds") or 0.0) > 1.0
        ]
        if not loads:
            return None
        recent = sorted(loads[-window:])
        return recent[len(recent) // 2]

    def describe_loading(self, model: str, for_seconds: float) -> str:
        """One line for a load in progress: a progress bar against the model's
        typical load time when history exists, honest elapsed time otherwise."""
        typical = self.typical_load_seconds(model)
        if typical:
            fraction = min(for_seconds / typical, 0.99)
            return (f"loading {model} {progress_bar(fraction)} {fraction:.0%} "
                    f"({for_seconds:.0f}s of ~{typical:.0f}s typical)")
        return f"loading {model} … {for_seconds:.0f}s (first load — learning its timing)"

    @staticmethod
    def describe(event: dict) -> str:
        ts = event.get("ts", "")[11:19]
        model = event.get("model", "?")
        kind = event.get("event")
        if kind == "ready":
            load = event.get("load_seconds", 0)
            suffix = f"loaded in {human_duration(load)}" if load else "ready"
            return f"{ts} {model} {suffix}"
        if kind == "unloaded":
            return f"{ts} {model} unloaded after {human_duration(event.get('resident_seconds', 0))}"
        if kind == "aborted":
            return f"{ts} {model} load aborted after {human_duration(event.get('loading_seconds', 0))}"
        return f"{ts} {model} loading …"
