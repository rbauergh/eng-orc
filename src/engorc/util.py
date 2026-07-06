"""Small dependency-free helpers: ids, time, text shaping."""

from __future__ import annotations

import re
import secrets
import time
from datetime import UTC, datetime

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str = "") -> str:
    """Sortable ULID-style id: 9 chars of millisecond timestamp + 8 random chars."""
    ts = _b32(time.time_ns() // 1_000_000, 9)
    rand = _b32(secrets.randbits(40), 8)
    return f"{prefix}_{ts}{rand}" if prefix else f"{ts}{rand}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def human_age(ts: str) -> str:
    """Compact age like '3m', '2h', '5d' for status tables."""
    try:
        delta = utc_now() - parse_iso(ts)
    except ValueError:
        return "?"
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    for limit, suffix, div in ((120, "s", 1), (7200, "m", 60), (172800, "h", 3600)):
        if seconds < limit:
            return f"{seconds // div}{suffix}"
    return f"{seconds // 86400}d"


def human_duration(seconds: float) -> str:
    """Compact duration like '22s', '4m38s', '1h12m'."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].rstrip("-") or "project"


def shorten(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate_middle(text: str, max_chars: int, marker: str = "\n…[{n} chars elided]…\n") -> str:
    """Keep the head and tail of oversized text; the middle is usually noise."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    elided = len(text) - 2 * keep
    return text[:keep] + marker.format(n=elided) + text[-keep:]


def truncate_tail(text: str, max_chars: int, marker: str = "…[{n} chars elided]…\n") -> str:
    """Keep only the tail (test runs, tracebacks: the end matters most)."""
    if len(text) <= max_chars:
        return text
    elided = len(text) - max_chars
    return marker.format(n=elided) + text[-max_chars:]


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def approx_tokens(text: str) -> int:
    """Cheap token estimate (~3.6 chars/token for code-heavy English) used when
    the server /tokenize endpoint is unavailable. Deliberately pessimistic."""
    return max(1, int(len(text) / 3.6))
