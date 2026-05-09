"""Five-view materializers (PLAN §8)."""
from __future__ import annotations

from pathlib import Path

import pytest

from artifactstore import ArtifactStore
from artifactstore.views import VIEW_NAMES, VIEWS

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def _seed(tmp_path: Path) -> tuple[ArtifactStore, str]:
    store = ArtifactStore.init(tmp_path / "store.db")
    raw = (FIXTURES / "pytest_auth_expiry.log").read_text()
    aid = store.put_artifact(
        tool_name="pytest", artifact_type="pytest_failure",
        raw_text=raw, creator_agent_id="t", session_id="s",
    )
    return store, aid


def test_view_names_complete():
    assert set(VIEW_NAMES) == {
        "preview", "evidence", "redacted", "raw", "provenance",
    }
    assert set(VIEWS.keys()) == set(VIEW_NAMES)


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_each_view_returns_string(tmp_path: Path, view: str):
    store, aid = _seed(tmp_path)
    out = VIEWS[view](store.conn, aid, token_budget=500)
    assert isinstance(out, str)
    if view != "evidence":
        # evidence may be empty if all spans exceed the budget; others must
        # produce content for our seeded fixture.
        assert out, f"view={view} returned empty"


def test_preview_includes_header(tmp_path: Path):
    store, aid = _seed(tmp_path)
    out = VIEWS["preview"](store.conn, aid, token_budget=500)
    assert out.startswith("[pytest_failure")
    assert "tokens]" in out.splitlines()[0]


def test_redacted_strips_jwt(tmp_path: Path):
    store = ArtifactStore.init(tmp_path / "store.db")
    raw = "log: token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature123abc"
    aid = store.put_artifact(
        tool_name="t", artifact_type="generic", raw_text=raw,
        creator_agent_id="t", session_id="s",
    )
    out = VIEWS["redacted"](store.conn, aid, token_budget=500)
    assert "REDACTED" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_provenance_is_json(tmp_path: Path):
    import json
    store, aid = _seed(tmp_path)
    out = VIEWS["provenance"](store.conn, aid, token_budget=2000)
    payload = json.loads(out)
    assert payload["artifact_id"] == aid
    assert payload["artifact_type"] == "pytest_failure"
    assert "raw_hash" in payload
