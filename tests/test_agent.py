"""Agent constructor safety + provider configuration.

The agent uses the `anthropic` SDK as a transport against any
Anthropic-Messages-API-compatible endpoint. Supported providers:
DeepSeek and Qwen (via Alibaba Model Studio). None of these tests
touch the network — they verify the wiring picks up the right env vars
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


PROVIDER_ENV_VARS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
    # Cleared too in case something in the parent shell exports a stale
    # Anthropic env — must NOT influence the resolver.
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
)


def _clear_env(monkeypatch):
    """Drop every provider env var so the resolver only sees what the
    test sets explicitly."""
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_agent_requires_api_key_when_no_client_injected(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(RuntimeError, match=r"DEEPSEEK_API_KEY|API key"):
        Agent(name="x", system="", tools=[])


def test_agent_accepts_injected_client(monkeypatch):
    _clear_env(monkeypatch)
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


def test_deepseek_base_url_env_overrides_default(monkeypatch):
    """DEEPSEEK_BASE_URL overrides the provider's hard-coded default —
    required for self-hosted DeepSeek shims or regional endpoints."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://my-deepseek-proxy.example/")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="deepseek-v4-pro"))
    assert "my-deepseek-proxy.example" in str(a.client.base_url)


def test_anthropic_env_does_not_leak_into_deepseek(monkeypatch):
    """A legacy ANTHROPIC_BASE_URL or ANTHROPIC_API_KEY in the shell rc
    must NOT silently apply to deepseek-* models. Each provider reads
    only its own env vars."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "real-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    # Decoy: must be ignored.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://decoy.example/")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="deepseek-v4-pro"))
    assert "deepseek.com" in str(a.client.base_url)
    assert "decoy.example" not in str(a.client.base_url)


def test_anthropic_env_does_not_leak_into_qwen(monkeypatch):
    """Critical: ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY in the shell
    rc must NOT silently route a Qwen request to Anthropic / DeepSeek.
    The user would have no way to know."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("QWEN_API_KEY", "real-qwen-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="qwen3.6-plus"))
    assert "dashscope" in str(a.client.base_url)
    assert "deepseek.com" not in str(a.client.base_url)


def test_modelconfig_base_url_overrides_env(monkeypatch):
    """An explicit ModelConfig(base_url=...) wins over every env var."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://elsewhere.example/")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(base_url=DEEPSEEK_BASE_URL))
    assert "deepseek.com" in str(a.client.base_url)
    assert "elsewhere" not in str(a.client.base_url)


def test_deepseek_model_picks_deepseek_url_with_no_env_override(monkeypatch):
    """Default deepseek-* model with no env-var override resolves to
    DeepSeek's URL automatically."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="deepseek-v4-pro"))
    assert "deepseek.com" in str(a.client.base_url)


def test_qwen_model_picks_qwen_url(monkeypatch):
    """Qwen model prefix routes to the DashScope intl endpoint by
    default."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("QWEN_API_KEY", "test-key-not-used")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="qwen3.6-plus"))
    assert "dashscope-intl.aliyuncs.com" in str(a.client.base_url)
    assert "/apps/anthropic" in str(a.client.base_url)


def test_qwen_base_url_env_overrides_default(monkeypatch):
    """QWEN_BASE_URL wins over the hard-coded default — required so
    mainland-CN tenants and self-hosted shims can point elsewhere."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("QWEN_API_KEY", "test-key-not-used")
    monkeypatch.setenv("QWEN_BASE_URL",
                       "https://dashscope.aliyuncs.com/apps/anthropic")
    a = Agent(name="x", system="", tools=[],
              config=ModelConfig(model="qwen3.6-plus"))
    assert "dashscope.aliyuncs.com/apps/anthropic" in str(a.client.base_url)
    assert "dashscope-intl" not in str(a.client.base_url)


def test_unknown_model_prefix_rejected(monkeypatch):
    """Models that don't start with deepseek- or qwen are rejected
    up-front — no silent fallback to a default provider that would
    misroute credentials."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    monkeypatch.setenv("QWEN_API_KEY", "test-key-not-used")
    with pytest.raises(RuntimeError, match="No provider matches"):
        Agent(name="x", system="", tools=[],
              config=ModelConfig(model="claude-sonnet-4-5"))
    with pytest.raises(RuntimeError, match="No provider matches"):
        Agent(name="x", system="", tools=[],
              config=ModelConfig(model="gpt-4o"))


def test_deepseek_key_missing_for_deepseek_model(monkeypatch):
    """Without DEEPSEEK_API_KEY the resolver must raise — must NOT
    silently use a QWEN_API_KEY or ANTHROPIC_API_KEY from the shell."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("QWEN_API_KEY", "leaked-qwen-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-anthropic-key")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        Agent(name="x", system="", tools=[],
              config=ModelConfig(model="deepseek-v4-pro"))


def test_qwen_key_required_for_qwen_models(monkeypatch):
    """Symmetric: Qwen model with only DEEPSEEK_API_KEY set must error,
    not silently send DeepSeek credentials to DashScope."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    with pytest.raises(RuntimeError, match="QWEN_API_KEY"):
        Agent(name="x", system="", tools=[],
              config=ModelConfig(model="qwen3.6-plus"))


def test_resolver_uses_correct_key_per_provider(monkeypatch):
    """Both keys set simultaneously: deepseek-* model must pick up
    DEEPSEEK_API_KEY, qwen* model must pick up QWEN_API_KEY — never
    the other way around. The one .env / two providers contract."""
    from demo.providers import resolve
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DK-deepseek")
    monkeypatch.setenv("QWEN_API_KEY", "QK-qwen")

    key_ds, url_ds, prov_ds = resolve("deepseek-v4-pro")
    assert key_ds == "DK-deepseek"
    assert "deepseek.com" in url_ds
    assert prov_ds.name == "DeepSeek"

    key_q, url_q, prov_q = resolve("qwen3.6-plus")
    assert key_q == "QK-qwen"
    assert "dashscope-intl.aliyuncs.com" in url_q
    assert "Qwen" in prov_q.name


def test_model_shorthand_expands_from_env(monkeypatch):
    """`--model deepseek` expands to $DEEPSEEK_MODEL; `--model qwen`
    expands to $QWEN_MODEL. Concrete ids pass through unchanged.
    Both providers' MODEL env vars are read independently — no leak."""
    from demo.providers import resolve_model_shorthand
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("QWEN_MODEL", "qwen3.6-plus")
    assert resolve_model_shorthand("deepseek") == "deepseek-v4-pro"
    assert resolve_model_shorthand("qwen") == "qwen3.6-plus"
    # Concrete ids untouched.
    assert resolve_model_shorthand("deepseek-v4-flash") == "deepseek-v4-flash"
    assert resolve_model_shorthand("qwen3-coder-plus") == "qwen3-coder-plus"


def test_model_shorthand_raises_when_env_missing(monkeypatch):
    """`--model qwen` with no $QWEN_MODEL must raise, not silently
    default — an empty .env value is operator error, not a quiet
    fallback."""
    from demo.providers import resolve_model_shorthand, ProviderError
    _clear_env(monkeypatch)
    monkeypatch.delenv("QWEN_MODEL", raising=False)
    with pytest.raises(ProviderError, match="QWEN_MODEL"):
        resolve_model_shorthand("qwen")
    monkeypatch.setenv("QWEN_MODEL", "   ")  # whitespace counts as empty
    with pytest.raises(ProviderError, match="QWEN_MODEL"):
        resolve_model_shorthand("qwen")


def test_describe_reports_unknown_provider(monkeypatch):
    """describe() must NOT raise on an unknown model — it must return
    a structured 'UNKNOWN' so the runner can print a clean error
    instead of stack-tracing past it."""
    from demo.providers import describe
    _clear_env(monkeypatch)
    d = describe("claude-sonnet-4-5")
    assert d["provider"] == "UNKNOWN"
    assert d["key_present"] is False
    assert "error" in d


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
