"""Span extractor registry + per-type extractors against captured fixtures."""
from __future__ import annotations

from pathlib import Path

from artifactstore.extractors import _REGISTRY, extract, register

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_required_types_registered():
    for t in ("pytest_failure", "grep_result", "git_diff"):
        assert t in _REGISTRY, f"missing extractor for {t!r}"


def test_unknown_type_returns_empty():
    assert extract("does_not_exist", "raw") == []


def test_pytest_extractor_finds_assertion():
    raw = (FIXTURES / "pytest_auth_expiry.log").read_text()
    spans = extract("pytest_failure", raw)
    span_types = {s[0] for s in spans}
    assert "assertion" in span_types
    assert any("token expired prematurely" in s[4] for s in spans), \
        "did not find the failing assertion text"


def test_pytest_extractor_finds_log_warning():
    """The 'now=... exp=...' line is the smoking gun for the timezone bug."""
    raw = (FIXTURES / "pytest_auth_expiry.log").read_text()
    spans = extract("pytest_failure", raw)
    log_spans = [s for s in spans if s[0] == "log_warning"]
    assert log_spans, "no log_warning span extracted"
    assert any("now=" in s[4] and "exp=" in s[4] for s in log_spans)


def test_grep_extractor_parses_path_line_text():
    raw = (FIXTURES / "rg_grep_noise.txt").read_text()
    spans = extract("grep_result", raw)
    # Anchor on the test_auth.py:84 hit.
    matches = [s for s in spans
               if s[1] == "tests/test_auth.py" and s[2] == 84]
    assert matches, "did not surface tests/test_auth.py:84"
    assert "token expired prematurely" in matches[0][4]


def test_grep_extractor_lowers_importance_for_todos():
    """TODO/FIXME hits are noise for diagnostic queries — importance is lower
    than ordinary lines so the omit-fits ordering surfaces real evidence first.
    """
    raw = (FIXTURES / "rg_grep_noise.txt").read_text()
    spans = extract("grep_result", raw)
    noise_imps = [s[5] for s in spans
                  if "TODO" in s[4] or "FIXME" in s[4]]
    real_imps = [s[5] for s in spans
                 if "TODO" not in s[4] and "FIXME" not in s[4]]
    assert noise_imps and real_imps
    assert max(noise_imps) < min(real_imps)


def test_git_diff_extractor_finds_changed_lines():
    raw = (FIXTURES / "git_diff_auth_refactor.diff").read_text()
    spans = extract("git_diff", raw)
    span_types = {s[0] for s in spans}
    assert "changed_line" in span_types
    paths = {s[1] for s in spans if s[0] == "changed_line"}
    assert "app/auth.py" in paths
    # The fix introduces datetime.now(timezone.utc).
    utc_lines = [s for s in spans if s[0] == "changed_line"
                 and "datetime.now(timezone.utc)" in s[4]]
    assert utc_lines, "expected the timezone.utc fix line in the diff"


def test_register_decorator_replaces():
    """Confirm register() replaces, then restore for siblings."""
    from artifactstore import extractors as ex
    original = ex._REGISTRY["pytest_failure"]
    @register("pytest_failure")
    def _new(_raw):
        return [("assertion", "x.py", 1, 1, "boom", 1.0)]
    try:
        spans = extract("pytest_failure", "anything")
        assert spans == [("assertion", "x.py", 1, 1, "boom", 1.0)]
    finally:
        ex._REGISTRY["pytest_failure"] = original
