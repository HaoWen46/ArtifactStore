"""Citation parsing + existence verification (artifactstore/cite.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from artifactstore.cite import (
    BadCitation,
    CITATION_RE,
    parse,
    verify_all,
    verify_resolves,
)


def test_parse_well_formed():
    art, span = parse("art_deadbeef/span_cafe1234")
    assert art == "art_deadbeef"
    assert span == "span_cafe1234"


@pytest.mark.parametrize("bad", [
    "",
    "art_x/span_x",
    "art_DEADBEEF/span_cafe1234",  # uppercase hex
    "artdeadbeef/spancafe1234",
    "art_deadbeef span_cafe1234",
    "art_deadbeef/span_cafe1234/extra",
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(BadCitation):
        parse(bad)


def test_regex_anchored():
    assert CITATION_RE.match("art_12345678/span_abcdef01")
    assert not CITATION_RE.match("prefix art_12345678/span_abcdef01")


def test_verify_resolves_against_real_db(tmp_path: Path):
    """Existence check uses artifact_spans rows. We insert one directly so the
    test does not depend on put_artifact (build step 1) being implemented."""
    store = ArtifactStore.init(tmp_path / "store.db")
    store.conn.execute(
        "INSERT INTO artifacts(artifact_id, session_id, artifact_type, raw_hash) "
        "VALUES (?, ?, ?, ?)",
        ("art_aaaaaaaa", "sess", "pytest_failure", "deadbeef"),
    )
    store.conn.execute(
        "INSERT INTO artifact_spans(span_id, artifact_id, span_type, text) "
        "VALUES (?, ?, ?, ?)",
        ("span_bbbbbbbb", "art_aaaaaaaa", "assertion", "boom"),
    )
    assert verify_resolves(store.conn, "art_aaaaaaaa/span_bbbbbbbb") is True
    assert verify_resolves(store.conn, "art_aaaaaaaa/span_99999999") is False
    assert verify_resolves(store.conn, "garbage") is False


def test_verify_all_preserves_order(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    out = verify_all(store.conn, ["bad", "art_xx/span_yy"])
    assert list(out.keys()) == ["bad", "art_xx/span_yy"]
    assert out == {"bad": False, "art_xx/span_yy": False}
