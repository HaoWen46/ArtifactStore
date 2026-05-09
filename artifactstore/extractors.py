"""Span extractors per artifact_type.

Extractor contract: callable(raw: str) -> Iterable[Span]
where Span = (span_type, file_path|None, line_start|None, line_end|None,
              text, importance: float in [0,1]).

The registry is type-keyed so a new artifact_type = a new extractor; never
branch inside a god function.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable

Span = tuple[str, str | None, int | None, int | None, str, float]
Extractor = Callable[[str], Iterable[Span]]

_REGISTRY: dict[str, Extractor] = {}


def register(artifact_type: str):
    def deco(fn: Extractor) -> Extractor:
        _REGISTRY[artifact_type] = fn
        return fn
    return deco


def extract(artifact_type: str, raw: str) -> list[Span]:
    fn = _REGISTRY.get(artifact_type)
    if fn is None:
        return []
    return list(fn(raw))


# --- pytest_failure -------------------------------------------------------
# Anchor on three lexical signals from real pytest output:
#   * `>           assert ...`              (the failing assertion line)
#   * `E           AssertionError: ...`     (the exception message)
#   * `WARNING  ...`                        (Captured log call lines)
# Top stack frame: the `path/to/test.py:NN: AssertionError` summary line.

_PYTEST_ASSERTION_LINE = re.compile(r"^>\s+(.*)$", re.MULTILINE)
_PYTEST_ERROR_LINE = re.compile(r"^E\s+(.*)$", re.MULTILINE)
_PYTEST_TOP_FRAME = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+):\s+(?P<msg>.+)$", re.MULTILINE
)
_PYTEST_LOG_LINE = re.compile(
    r"^(?P<level>WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[^\s:]+):(?P<file>[^:]+):(?P<line>\d+)\s+(?P<msg>.+)$",
    re.MULTILINE,
)


@register("pytest_failure")
def _pytest(raw: str) -> Iterable[Span]:
    for m in _PYTEST_ASSERTION_LINE.finditer(raw):
        yield ("assertion", None, None, None, m.group(1).strip(), 0.95)
    for m in _PYTEST_ERROR_LINE.finditer(raw):
        yield ("error_message", None, None, None, m.group(1).strip(), 0.9)
    for m in _PYTEST_TOP_FRAME.finditer(raw):
        # Only surface frames that include an AssertionError or similar; skip
        # the "1 failed, 46 passed" footer which also matches path:line:msg.
        msg = m.group("msg")
        if "Error" not in msg and "assert" not in msg.lower():
            continue
        yield ("stack_frame",
               m.group("path"),
               int(m.group("line")),
               int(m.group("line")),
               m.group(0).strip(),
               0.85)
    for m in _PYTEST_LOG_LINE.finditer(raw):
        yield ("log_warning",
               m.group("file"),
               int(m.group("line")),
               int(m.group("line")),
               m.group(0).strip(),
               0.75)


# --- grep_result ----------------------------------------------------------
# Standard `path:line:text` rg output. Importance leans on whether the line
# looks like it carries TODO/FIXME (lower; noise) vs actual logic (higher).

_GREP_LINE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<text>.*)$",
                        re.MULTILINE)


@register("grep_result")
def _grep(raw: str) -> Iterable[Span]:
    for m in _GREP_LINE.finditer(raw):
        text = m.group("text")
        importance = 0.4 if re.search(r"\b(TODO|FIXME)\b", text) else 0.7
        yield ("grep_hit",
               m.group("path"),
               int(m.group("line")),
               int(m.group("line")),
               text.strip(),
               importance)


# --- git_diff -------------------------------------------------------------
# Walk hunks; each + or - line (excluding +++/--- headers) becomes one
# changed_line span. file_path tracks the most recent +++ b/<path> header.

_DIFF_FILE_HEADER = re.compile(r"^\+\+\+\s+b/(.+)$")
_DIFF_HUNK_HEADER = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


@register("git_diff")
def _git_diff(raw: str) -> Iterable[Span]:
    cur_path: str | None = None
    new_lineno = 0
    for line in raw.splitlines():
        if line.startswith("+++"):
            m = _DIFF_FILE_HEADER.match(line)
            cur_path = m.group(1) if m else None
            continue
        if line.startswith("---") or line.startswith("diff --git"):
            continue
        if line.startswith("@@"):
            m = _DIFF_HUNK_HEADER.match(line)
            if m:
                new_lineno = int(m.group(2))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            yield ("changed_line", cur_path, new_lineno, new_lineno,
                   line[1:].rstrip(), 0.8)
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            yield ("changed_line", cur_path, new_lineno, new_lineno,
                   line[1:].rstrip(), 0.7)
            # deletion does not advance new_lineno
        else:
            # context line; advance new_lineno only
            new_lineno += 1
