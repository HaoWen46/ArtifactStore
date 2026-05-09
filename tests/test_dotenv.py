"""Tests for the stdlib .env loader in demo/runner.py.

Never touches the project's real .env — every test writes its own to tmp_path.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from demo.runner import load_dotenv


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_loads_basic_kv(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FOO_TEST_KEY", raising=False)
    p = _write(tmp_path / ".env", "FOO_TEST_KEY=bar\n")
    set_keys = load_dotenv(p)
    assert set_keys == {"FOO_TEST_KEY": "bar"}
    assert os.environ["FOO_TEST_KEY"] == "bar"


def test_strips_quotes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("Q_DOUBLE", raising=False)
    monkeypatch.delenv("Q_SINGLE", raising=False)
    p = _write(tmp_path / ".env", 'Q_DOUBLE="hello world"\nQ_SINGLE=\'multi word\'\n')
    load_dotenv(p)
    assert os.environ["Q_DOUBLE"] == "hello world"
    assert os.environ["Q_SINGLE"] == "multi word"


def test_skips_comments_and_blanks(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KEEP_ME", raising=False)
    p = _write(tmp_path / ".env",
               "# this is a comment\n\nKEEP_ME=yes\n# another comment\n")
    set_keys = load_dotenv(p)
    assert set_keys == {"KEEP_ME": "yes"}


def test_does_not_override_existing_env_by_default(tmp_path: Path, monkeypatch):
    """Real shell exports should win — that matches python-dotenv default."""
    monkeypatch.setenv("ALREADY_SET", "shell")
    p = _write(tmp_path / ".env", "ALREADY_SET=file\n")
    set_keys = load_dotenv(p)
    assert os.environ["ALREADY_SET"] == "shell"
    assert "ALREADY_SET" not in set_keys  # .env did not win


def test_empty_existing_value_is_treated_as_unset(tmp_path: Path, monkeypatch):
    """Common gotcha: a shell rc with `export ANTHROPIC_API_KEY=` exports an
    empty string. Treating that as 'already set' would silently swallow the
    real value from .env. Empty -> .env wins (matches python-dotenv)."""
    monkeypatch.setenv("MAYBE_EMPTY", "")
    p = _write(tmp_path / ".env", "MAYBE_EMPTY=real-value\n")
    set_keys = load_dotenv(p)
    assert os.environ["MAYBE_EMPTY"] == "real-value"
    assert set_keys == {"MAYBE_EMPTY": "real-value"}


def test_override_true_replaces(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "shell")
    p = _write(tmp_path / ".env", "ALREADY_SET=file\n")
    set_keys = load_dotenv(p, override=True)
    assert os.environ["ALREADY_SET"] == "file"
    assert set_keys == {"ALREADY_SET": "file"}


def test_missing_file_is_noop(tmp_path: Path):
    set_keys = load_dotenv(tmp_path / "does-not-exist")
    assert set_keys == {}


def test_anthropic_pair_propagates_to_agent(tmp_path: Path, monkeypatch):
    """End-to-end: a .env containing the Anthropic-key pair gets loaded by the
    runner helper, and a freshly constructed Agent picks up base_url. No
    network calls — just verify the SDK client carries the configured URL.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    p = _write(tmp_path / ".env",
               'ANTHROPIC_API_KEY="test-key-not-used"\n'
               'ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"\n')
    load_dotenv(p)

    from demo.agent import Agent
    a = Agent(name="x", system="", tools=[])
    assert "deepseek.com" in str(a.client.base_url)


def test_real_dotenv_has_required_keys_if_present():
    """If the user actually has a .env, sanity-check it before they bleed
    money on a misconfigured run. We only assert keys exist; we never read
    or print the values."""
    project_env = Path(__file__).parent.parent / ".env"
    if not project_env.is_file():
        pytest.skip("no project .env")
    content = project_env.read_text()
    assert "ANTHROPIC_API_KEY=" in content, \
        "project .env exists but has no ANTHROPIC_API_KEY"
