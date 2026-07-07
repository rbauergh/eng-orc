"""Interactive session presence.

`orc new -i` and `orc request -i` hold a conversation with the model OUTSIDE
any project: nothing lands in a project journal, so without a presence record
the dashboard reads "idle" while the GPU is visibly working, and a concurrent
`orc run --watch` will happily swap the conversation's model out between
turns. Sessions follow the filesystem-is-the-database rule: one JSON file per
live interactive session under <home>/sessions/, updated on every status
change, removed on exit, and cleaned up when its process is gone.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from .fsio import atomic_write_json, read_json
from .util import iso_now


def _sessions_dir(home: Path) -> Path:
    path = home / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class InteractiveSession:
    def __init__(self, path: Path, record: dict):
        self._path = path
        self._record = record

    def update(self, status: str) -> None:
        self._record["status"] = status
        self._record["updated"] = iso_now()
        atomic_write_json(self._path, self._record)


@contextmanager
def interactive_session(home: Path, kind: str, detail: str = ""):
    """Advertise a live interactive session; the file exists exactly as long
    as the conversation does (early returns and exceptions included)."""
    pid = os.getpid()
    path = _sessions_dir(home) / f"{kind}-{pid}.json"
    record = {"kind": kind, "detail": detail, "pid": pid,
              "started": iso_now(), "updated": iso_now(), "status": "starting"}
    atomic_write_json(path, record)
    session = InteractiveSession(path, record)
    try:
        yield session
    finally:
        path.unlink(missing_ok=True)


def active_sessions(home: Path) -> list[dict]:
    """Live sessions only; files left by dead processes are removed as seen."""
    root = home / "sessions"
    if not root.is_dir():
        return []
    sessions: list[dict] = []
    for path in sorted(root.glob("*.json")):
        record = read_json(path, default=None)
        if not isinstance(record, dict):
            continue
        try:
            pid = int(record.get("pid", -1))
        except (TypeError, ValueError):
            pid = -1
        if not _pid_alive(pid):
            path.unlink(missing_ok=True)
            continue
        sessions.append(record)
    return sessions


def foreign_sessions(home: Path) -> list[dict]:
    """Active interactive sessions belonging to OTHER processes — the ones a
    scheduler should yield the GPU to."""
    me = os.getpid()
    return [s for s in active_sessions(home) if s.get("pid") != me]
