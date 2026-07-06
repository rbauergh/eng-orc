"""Filesystem primitives: atomic writes, JSONL append/iterate, advisory locks.

Every mutation of on-disk state in eng-orc goes through these helpers so that
a crash at any point leaves either the old file or the new file, never a
partial write, and concurrent processes (scheduler + CLI) cannot interleave
appends.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_yaml(path: Path, obj: Any) -> None:
    text = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)
    atomic_write_text(path, text)


def read_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if loaded is None else loaded


@contextmanager
def flocked(path: Path):
    """Advisory exclusive lock scoped to a sidecar .lock file."""
    ensure_dir(path.parent)
    lock_path = path.parent / f".{path.name}.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def append_jsonl(path: Path, obj: Any) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    with flocked(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line from a crashed writer is skippable noise


class FileLock:
    """Blocking inter-process lock with owner metadata, used as the GPU lease.

    Only one component may drive the LLM server at a time (a single 12 GB card
    cannot serve two heavyweight requests without thrashing the swap proxy).
    """

    def __init__(self, path: Path, timeout: float = 3600.0, poll: float = 0.5):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fh = None

    def acquire(self, label: str = "") -> None:
        ensure_dir(self.path.parent)
        deadline = time.monotonic() + self.timeout
        self._fh = open(self.path, "a+")
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() > deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(
                        f"could not acquire lock {self.path} within {self.timeout}s"
                    ) from None
                time.sleep(self.poll)
        self._fh.truncate(0)
        self._fh.write(f"pid={os.getpid()} label={label}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
