"""In-process CLI tests using typer.testing.CliRunner.

Complements tests/test_cli.py (subprocess-based) by exercising the same
command surface in-process — so pytest-cov instrumentation actually
sees the cli.py code paths. Subprocess-based tests catch packaging /
arg-parsing regressions; in-process tests give us coverage credit and
faster iteration.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from artifactstore.cli import app

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False → stderr is reachable separately; needed because
    # we test that AccessDenied / unresolved-citation messages appear in
    # stderr, not stdout.
    return CliRunner()


def _invoke(runner: CliRunner, *args: str, expect_exit: int = 0):
    result = runner.invoke(app, list(args))
    if result.exit_code != expect_exit:
        raise AssertionError(
            f"unexpected exit {result.exit_code} (wanted {expect_exit})\n"
            f"args: {args}\n"
            f"output:\n{result.output}\n"
            f"exception: {result.exception!r}"
        )
    return result


# --- happy path: init, put, grant, search, expand, audit ------------------

def test_init_creates_db(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    r = _invoke(runner, "init", "--db", str(db))
    assert "initialized" in r.output
    assert db.exists()


def test_full_loop_in_process(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    aid = _invoke(
        runner, "put", str(FIXTURES / "pytest_auth_expiry.log"),
        "--tool", "pytest", "--type", "pytest_failure",
        "--db", str(db),
    ).output.strip()
    assert re.fullmatch(r"art_[0-9a-f]{8}", aid)

    gid = _invoke(
        runner, "grant", "--agent", "worker",
        "--types", "pytest_failure", "--views", "preview,evidence",
        "--ops", "search,get_spans,expand_view",
        "--ttl", "30m", "--db", str(db),
    ).output.strip()
    assert gid.startswith("grant_")

    rows = json.loads(
        _invoke(runner, "search", "expired", "--grant", gid,
                "--db", str(db)).output
    )
    assert rows and rows[0]["artifact_id"] == aid


# --- new verbs: verify, find-related, show --------------------------------

def test_verify_resolved_returns_zero(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    aid = _invoke(
        runner, "put", str(FIXTURES / "pytest_auth_expiry.log"),
        "--tool", "pytest", "--type", "pytest_failure", "--db", str(db),
    ).output.strip()
    show_out = json.loads(_invoke(runner, "show", aid, "--db", str(db)).output)
    span_id = show_out["spans"][0]["span_id"]
    citation = f"{aid}/{span_id}"
    r = _invoke(runner, "verify", citation, "--db", str(db))
    assert "resolved:" in r.output


def test_verify_unresolved_returns_4(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    r = _invoke(runner, "verify", "art_deadbeef/span_cafef00d",
                 "--db", str(db), expect_exit=4)
    assert "unresolved" in r.output


def test_verify_malformed_returns_5(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    r = _invoke(runner, "verify", "not-a-citation", "--db", str(db),
                 expect_exit=5)
    assert "malformed" in r.output


def test_find_related_returns_empty_when_no_links(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    aid = _invoke(
        runner, "put", str(FIXTURES / "pytest_auth_expiry.log"),
        "--tool", "pytest", "--type", "pytest_failure", "--db", str(db),
    ).output.strip()
    rows = json.loads(_invoke(
        runner, "find-related", aid, "--grant", "__supervisor__",
        "--db", str(db),
    ).output)
    assert rows == []


def test_show_unknown_artifact_exits_2(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    r = _invoke(runner, "show", "art_99999999", "--db", str(db),
                 expect_exit=2)
    assert "no such artifact" in r.output or "no such artifact" in str(r.exception)


def test_show_full_metadata(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    aid = _invoke(
        runner, "put", str(FIXTURES / "pytest_auth_expiry.log"),
        "--tool", "pytest", "--type", "pytest_failure", "--db", str(db),
    ).output.strip()
    payload = json.loads(_invoke(runner, "show", aid, "--db", str(db)).output)
    for key in ("artifact_id", "raw_hash", "spans", "outbound_links",
                "metadata", "span_count"):
        assert key in payload
    assert len(payload["raw_hash"]) == 64


# --- the access-denied path is the most important UX contract ------------

def test_expand_raw_under_evidence_only_grant_exits_3(runner, tmp_path: Path):
    db = tmp_path / "store.db"
    _invoke(runner, "init", "--db", str(db))
    aid = _invoke(
        runner, "put", str(FIXTURES / "pytest_auth_expiry.log"),
        "--tool", "pytest", "--type", "pytest_failure", "--db", str(db),
    ).output.strip()
    gid = _invoke(
        runner, "grant", "--agent", "w",
        "--types", "pytest_failure", "--views", "preview,evidence",
        "--ops", "expand_view", "--ttl", "30m", "--db", str(db),
    ).output.strip()
    r = _invoke(runner, "expand", aid, "--view", "raw",
                 "--grant", gid, "--db", str(db), expect_exit=3)
    # Denial message in output (typer's CliRunner combines stdout/stderr).
    assert "denied" in r.output or "denied" in str(r.exception)
