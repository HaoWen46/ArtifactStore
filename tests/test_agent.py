"""Agent constructor safety + provider configuration.

The agent uses the `anthropic` SDK as its HTTP client; via `ANTHROPIC_BASE_URL`
the same SDK speaks DeepSeek's Anthropic-compatible endpoint. None of these
tests touch the network — they verify the wiring picks up the right env vars
and that an injected client fully bypasses provider detection.
"""
from __future__ import annotations

import pytest

from demo.agent import (
    DEEPSEEK_BASE_URL,
    DEFAULT_MODEL,
    Agent,
    ModelConfig,
)


def test_agent_requires_api_key_when_no_client_injected(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Agent(name="x", system="", tools=[])


def test_agent_accepts_injected_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sentinel = object()
    a = Agent(name="x", system="", tools=[], client=sentinel,
              config=ModelConfig())
    assert a.client is sentinel


def test_default_model_is_deepseek():
    """The project default must be the cheap-by-default DeepSeek model.
    If you change this, update CLAUDE.md and PLAN §20.5 too."""
    assert DEFAULT_MODEL == "deepseek-v4-pro"
    assert ModelConfig().model == DEFAULT_MODEL


def test_deepseek_base_url_constant():
    assert DEEPSEEK_BASE_URL == "https://api.deepseek.com/anthropic"


def test_base_url_from_env_propagates_to_sdk(monkeypatch):
    """When ANTHROPIC_BASE_URL is set and no client is injected, the SDK
    client is constructed with that base_url. We don't make a network call —
    we just check the constructed client picked it up."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    a = Agent(name="x", system="", tools=[])
    # The Anthropic Python SDK exposes `.base_url` on the client.
    assert "deepseek.com" in str(a.client.base_url)


def test_modelconfig_base_url_overrides_env(monkeypatch):
    """An explicit ModelConfig(base_url=...) wins over the env var."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example/")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(base_url=DEEPSEEK_BASE_URL))
    assert "deepseek.com" in str(a.client.base_url)
    assert "elsewhere" not in str(a.client.base_url)


def test_no_base_url_uses_anthropic_default(monkeypatch):
    """No env var, no config override → SDK falls back to Anthropic's URL."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    a = Agent(name="x", system="", tools=[])
    # Anthropic's default is api.anthropic.com — sanity check we didn't
    # accidentally hardwire DeepSeek as the base_url.
    assert "deepseek.com" not in str(a.client.base_url)


# ---------------------------------------------------------------------------
# max_turns hard cap. Required because tool_choice can't reliably force
# submit_report on DeepSeek (rejected with 400 for reasoning models). The cap
# is the actual safety net.
# ---------------------------------------------------------------------------

def _looping_client(*, force_text_at_turn: int | None = None):
    """A scripted client that always returns a tool_use until a target turn,
    after which it optionally returns text (end_turn). Models that never
    naturally terminate exercise the max_turns cap."""

    class Block:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Usage:
        input_tokens = 1
        output_tokens = 1

    class Resp:
        def __init__(self, content, stop_reason):
            self.content = content
            self.stop_reason = stop_reason
            self.usage = Usage()

    class C:
        def __init__(self):
            self.turns = 0
            self.last_kwargs: dict = {}

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            self.turns += 1
            if (force_text_at_turn is not None
                    and self.turns == force_text_at_turn):
                return Resp([Block(type="text", text="done.")], "end_turn")
            return Resp(
                [Block(type="tool_use", id=f"toolu_{self.turns}",
                       name="noop", input={})],
                "tool_use",
            )

    return C()


def test_max_turns_cap_forces_exit():
    """An agent that never naturally terminates must exit at max_turns with
    a synthetic AgentResult (stop_reason='max_turns')."""
    from demo.agent import Tool

    noop = Tool(name="noop", description="no-op",
                input_schema={"type": "object", "properties": {}},
                fn=lambda **_: "ok")
    client = _looping_client()
    a = Agent(name="loopy", system="", tools=[noop],
              config=ModelConfig(max_turns=4),
              client=client)
    r = a.run("go")
    assert r.stop_reason == "max_turns"
    assert r.turns == 4
    # Defensive: client should have been called exactly max_turns times,
    # not max_turns+1 (we exit BEFORE the next paid call).
    assert client.turns == 4


def test_natural_termination_before_cap():
    """If the model ends the turn naturally before the cap, we stop there."""
    from demo.agent import Tool

    noop = Tool(name="noop", description="no-op",
                input_schema={"type": "object", "properties": {}},
                fn=lambda **_: "ok")
    client = _looping_client(force_text_at_turn=3)
    a = Agent(name="x", system="", tools=[noop],
              config=ModelConfig(max_turns=10),
              client=client)
    r = a.run("go")
    assert r.stop_reason == "end_turn"
    assert r.turns == 3
    assert "done" in r.final_text


def test_tool_choice_falls_back_when_provider_rejects():
    """If the SDK raises with 'tool_choice' in the message, retry without it.
    This handles DeepSeek's reasoning model rejecting named tool_choice."""
    from demo.agent import Tool

    class Block:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Usage:
        input_tokens = 1
        output_tokens = 1

    class Resp:
        def __init__(self, content, stop_reason="end_turn"):
            self.content = content
            self.stop_reason = stop_reason
            self.usage = Usage()

    class C:
        def __init__(self):
            self.calls = []

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.calls.append(kwargs.get("tool_choice"))
            if "tool_choice" in kwargs:
                raise RuntimeError(
                    "deepseek-reasoner does not support this tool_choice"
                )
            return Resp([Block(type="text", text="ok")], "end_turn")

    noop = Tool(name="noop", description="no-op",
                input_schema={"type": "object", "properties": {}},
                fn=lambda **_: "ok")
    client = C()
    # Force-terminator AND a turn count high enough to trigger tool_choice.
    a = Agent(name="x", system="", tools=[noop],
              config=ModelConfig(max_turns=2),
              client=client,
              force_terminator="noop")
    r = a.run("go")
    # The agent should have retried without tool_choice and succeeded.
    assert r.stop_reason == "end_turn"
    # Two API calls: one with tool_choice (rejected), one without (succeeded).
    assert client.calls == [{"type": "any"}, None]
