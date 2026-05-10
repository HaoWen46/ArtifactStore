"""End-to-end CLI integration tests via subprocess.

We exercise the installed `artifactstore` console script (registered as
`[project.scripts]` in pyproject.toml). These tests catch regressions in
arg parsing, exit codes, and stdout/stderr shape that unit tests on the
underlying API would miss.

Each test runs against a tmp_path SQLite file so they're isolated.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def _run(*args: str, expect_exit: int = 0) -> subprocess.CompletedProcess:
    """Run `artifactstore <args>` via uv. Returns the CompletedProcess.
    Asserts exit code matches `expect_exit`."""
    cmd = ["uv", "run", "artifactstore", *args]
    p = subprocess.run(cmd, capture_output=True, text=True,
                        cwd=Path(__file__).parent.parent)
    if p.returncode != expect_exit:
        raise AssertionError(
            f"unexpected exit {p.returncode} (wanted {expect_exit})\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stdout:\n{p.stdout}\n"
            f"stderr:\n{p.stderr}"
        )
    return p


# ---------------------------------------------------------------------------
# Existing verbs — sanity that they still work post-refactor.
# ---------------------------------------------------------------------------

def test_cli_init_put_search_full_loop(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    aid = _run("put", str(FIXTURES / "pytest_auth_expiry.log"),
               "--tool", "pytest", "--type", "pytest_failure",
               "--db", str(db)).stdout.strip()
    assert re.fullmatch(r"art_[0-9a-f]{8}", aid), aid
    gid = _run("grant", "--agent", "worker",
               "--types", "pytest_failure",
               "--views", "preview,evidence",
               "--ops", "search,get_spans,expand_view",
               "--ttl", "30m", "--db", str(db)).stdout.strip()
    assert gid.startswith("grant_")
    out = _run("search", "expired", "--grant", gid,
                "--db", str(db)).stdout
    rows = json.loads(out)
    assert rows and rows[0]["artifact_id"] == aid


# ---------------------------------------------------------------------------
# verify — citation resolution (new verb)
# ---------------------------------------------------------------------------

def test_cli_verify_resolved_returns_exit_0(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    aid = _run("put", str(FIXTURES / "pytest_auth_expiry.log"),
               "--tool", "pytest", "--type", "pytest_failure",
               "--db", str(db)).stdout.strip()
    # Pull a real span_id via `show`.
    show_out = json.loads(
        _run("show", aid, "--db", str(db)).stdout
    )
    assert show_out["span_count"] > 0
    span_id = show_out["spans"][0]["span_id"]
    citation = f"{aid}/{span_id}"
    p = _run("verify", citation, "--db", str(db))
    assert "resolved:" in p.stdout
    assert aid in p.stdout
    assert span_id in p.stdout


def test_cli_verify_unresolved_returns_exit_4(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    p = _run("verify", "art_deadbeef/span_cafef00d", "--db", str(db),
              expect_exit=4)
    assert "unresolved" in p.stderr


def test_cli_verify_malformed_returns_exit_5(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    p = _run("verify", "not-a-valid-citation", "--db", str(db),
              expect_exit=5)
    assert "malformed" in p.stderr


# ---------------------------------------------------------------------------
# find-related — provenance traversal (new verb)
# ---------------------------------------------------------------------------

def test_cli_find_related_lists_links(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    parent = _run("put", str(FIXTURES / "pytest_auth_expiry.log"),
                   "--tool", "pytest", "--type", "pytest_failure",
                   "--db", str(db)).stdout.strip()
    # The `show` of the parent has zero outbound links.
    s1 = json.loads(_run("show", parent, "--db", str(db)).stdout)
    assert s1["outbound_links"] == []
    # find-related under the seeded supervisor grant returns []  also.
    rels = json.loads(_run("find-related", parent,
                            "--grant", "__supervisor__",
                            "--db", str(db)).stdout)
    assert rels == []


# ---------------------------------------------------------------------------
# show — debugging helper (new verb)
# ---------------------------------------------------------------------------

def test_cli_show_prints_full_metadata(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    aid = _run("put", str(FIXTURES / "pytest_auth_expiry.log"),
               "--tool", "pytest", "--type", "pytest_failure",
               "--db", str(db)).stdout.strip()
    payload = json.loads(_run("show", aid, "--db", str(db)).stdout)
    # All expected keys present.
    for key in ("artifact_id", "artifact_type", "raw_hash",
                "preview", "spans", "outbound_links",
                "metadata", "span_count"):
        assert key in payload, f"missing key in show output: {key}"
    assert payload["artifact_type"] == "pytest_failure"
    assert payload["span_count"] == len(payload["spans"])
    # raw_hash is sha256 hex (64 chars).
    assert len(payload["raw_hash"]) == 64


def test_cli_show_unknown_artifact_exits_2(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    p = _run("show", "art_99999999", "--db", str(db), expect_exit=2)
    assert "no such artifact" in p.stderr


# ---------------------------------------------------------------------------
# Denial path — AccessDenied → clean exit code 3, no traceback.
# ---------------------------------------------------------------------------

def test_cli_access_denied_exits_3_with_clean_message(tmp_path: Path):
    db = tmp_path / "store.db"
    _run("init", "--db", str(db))
    aid = _run("put", str(FIXTURES / "pytest_auth_expiry.log"),
               "--tool", "pytest", "--type", "pytest_failure",
               "--db", str(db)).stdout.strip()
    # Mint a grant that does NOT allow the 'raw' view.
    gid = _run("grant", "--agent", "worker",
               "--types", "pytest_failure",
               "--views", "preview,evidence",
               "--ops", "search,expand_view",
               "--ttl", "30m", "--db", str(db)).stdout.strip()
    p = _run("expand", aid, "--view", "raw", "--grant", gid,
              "--db", str(db), expect_exit=3)
    # stderr should be the one-line denied message — no traceback.
    assert "denied:" in p.stderr
    assert "Traceback" not in p.stderr
