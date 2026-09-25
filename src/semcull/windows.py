"""Pure interval and UTF-8 window operations; ranges are half-open bytes."""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterable

from .models import SemcullError

Range = tuple[int, int]


def merge(ranges: Iterable[Range]) -> list[Range]:
    result: list[Range] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def subtract(bounds: Range, covered: Iterable[Range]) -> list[Range]:
    cursor, end = bounds
    result = []
    for left, right in merge(covered):
        left, right = max(left, bounds[0]), min(right, end)
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            result.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        result.append((cursor, end))
    return result


def coverage(total: int, ranges: Iterable[Range]) -> dict:
    count = sum(b - a for a, b in merge(ranges))
    return {"evaluated_bytes": count, "total_bytes": total, "has_unexamined": count < total}


def parse_range(value: str, *, lines: bool = False) -> Range:
    if not re.fullmatch(r"\d+:\d+", value):
        raise SemcullError("invalid_range", "Use START:END integers.")
    start, end = map(int, value.split(":"))
    if start < (1 if lines else 0) or end < start:
        raise SemcullError("invalid_range", "Range is reversed or starts before the source.")
    return start, end


def parse_selector(value: str | None) -> tuple[str, Range | None]:
    if value is None:
        return "auto", None
    if value in ("tail", "next"):
        return value, None
    match = re.fullmatch(r"lines:(\d+)-(\d+)", value)
    if match:
        return "lines", parse_range(":".join(match.groups()), lines=True)
    raise SemcullError("invalid_selector", "Use tail, next, or lines:START-END for --only.")


def trim_utf8(data: bytes, start: int, *, tail: bool) -> tuple[int, bytes]:
    """Clip only the cut edge of an otherwise valid UTF-8 source slice."""
    if tail:
        removed = 0
        while removed < len(data) and data[removed] & 0xC0 == 0x80:
            removed += 1
        return start + removed, data[removed:]
    decoder = codecs.getincrementaldecoder("utf-8")()
    decoder.decode(data, final=False)
    pending, _ = decoder.getstate()
    return start, data[: len(data) - len(pending)]


def estimated_tokens(value: str) -> int:
    # Conservative UTF-8 byte estimate, not a claim of exact tokenization.
    return len(value.encode("utf-8"))
