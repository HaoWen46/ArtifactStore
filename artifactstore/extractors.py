"""Span extractors per artifact_type. Register a callable that yields
(span_type, file_path|None, line_start|None, line_end|None, text, importance)."""
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


# --- stubs: implement in build step 3 (PLAN §13) ---

@register("pytest_failure")
def _pytest(raw: str) -> Iterable[Span]:
    return ()


@register("grep_result")
def _grep(raw: str) -> Iterable[Span]:
    return ()


@register("git_diff")
def _git_diff(raw: str) -> Iterable[Span]:
    return ()
