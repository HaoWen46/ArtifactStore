"""Minimal tool-use agent loop, provider-agnostic.

Adapted from anthropic-quickstarts/agents (MIT). Stripped down: no MCP,
no async-everywhere. Tools are sync callables; we wrap them in to_thread
only if a tool ever needs it.

The client is the `anthropic` Python SDK used purely as an HTTP client
against an Anthropic-Messages-API-compatible endpoint. The default
endpoint is DeepSeek; Qwen (Alibaba Model Studio) is also supported.
The model name prefix picks the provider — see `demo/providers.py`.

Env vars consumed (when no client is injected):
    deepseek-* models -> DEEPSEEK_API_KEY [+ DEEPSEEK_BASE_URL]
    qwen* models      -> QWEN_API_KEY     [+ QWEN_BASE_URL]
No cross-provider fallback. Missing key for the resolved provider
raises a pointed RuntimeError up-front.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from anthropic import Anthropic

# Default model: DeepSeek V4 Pro. ~7-17x cheaper than Anthropic Sonnet 4.5
# at the time of writing (~$0.44/M input vs $3/M, ~$0.87/M output vs $15/M),
# and supports the same tool_use blocks via the /anthropic endpoint.
# Override via ModelConfig(model=...) or runner --model flag.
DEFAULT_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]  # called with **input, returns str | dict | list

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ModelConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    # Hard ceiling on agent loop turns. When exceeded, Agent.run returns with
    # stop_reason="max_turns" so callers (e.g. delegate) can detect non-natural
    # termination and react. Necessary because not every provider honors
    # `tool_choice: {type: tool, name: ...}` — DeepSeek's reasoning models
    # reject it with 400 — so we can't reliably force submit_report; we rely
    # on a strong prompt + this cap as the backstop.
    max_turns: int = 10
    temperature: float = 1.0
    # Explicit base_url override. None => resolved from the model name
    # via demo.providers (DEEPSEEK_BASE_URL / QWEN_BASE_URL env vars or
    # the provider's hard-coded default).
    base_url: str | None = None


@dataclass
class AgentResult:
    final_text: str
    stop_reason: str
    turns: int
    tool_calls: int
    # Token accounting. `input_tokens` is the SDK's "uncached" count (what's
    # billed at full rate); cache_read/cache_creation are populated when the
    # provider supports prompt caching (DeepSeek; Qwen may not). For
    # RQ1 efficiency comparisons across baselines, use `total_input_tokens`
    # — the actual tokens the model saw — not just `input_tokens`, which
    # gets warped by cache hits when reps share prompts.
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return (self.input_tokens
                + self.cache_read_input_tokens
                + self.cache_creation_input_tokens)


class Agent:
    def __init__(
        self,
        name: str,
        system: str,
        tools: list[Tool],
        config: ModelConfig | None = None,
        client: Anthropic | None = None,
        verbose: bool = False,
        force_terminator: str | None = None,
    ):
        self.name = name
        self.system = system
        self.tools = tools
        self.config = config or ModelConfig()
        if client is None:
            # Resolve provider from the model name so a sweep can run
            # `--model deepseek-v4-pro` and `--model qwen3.6-plus` back
            # to back without editing .env between runs. Caller-supplied
            # base_url still wins over the provider default.
            from demo.providers import resolve, ProviderError
            try:
                api_key, default_base_url, _ = resolve(self.config.model)
            except ProviderError as exc:
                raise RuntimeError(
                    f"{exc} Tests should inject a client= stub instead of "
                    "relying on env vars."
                ) from exc
            base_url = self.config.base_url or default_base_url
            client = Anthropic(api_key=api_key, base_url=base_url)
        self.client = client
        self.verbose = verbose
        self.force_terminator = force_terminator
        self._tool_dict = {t.name: t for t in tools}
        self.messages: list[dict[str, Any]] = []
        # Latched once we've observed the provider reject tool_choice
        # (Qwen3.6 thinking mode, DeepSeek-reasoner). Skips sending it on
        # later turns to avoid the wasted-retry round-trip.
        self._tool_choice_unsupported: bool = False

    def _exec_tool(self, call) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id}
        try:
            tool = self._tool_dict[call.name]
        except KeyError:
            block["content"] = f"Tool '{call.name}' not registered"
            block["is_error"] = True
            return block
        try:
            result = tool.fn(**call.input)
            block["content"] = result if isinstance(result, str) else json.dumps(result)
        except Exception as e:  # noqa: BLE001 — surface to model on purpose
            block["content"] = f"{type(e).__name__}: {e}"
            block["is_error"] = True
        return block

    def run(self, user_input: str) -> AgentResult:
        self.messages.append({"role": "user", "content": user_input})
        in_tok = out_tok = cache_read = cache_create = 0
        turns = tool_calls = 0

        while True:
            turns += 1
            if turns > self.config.max_turns:
                # Hard cap. Loop didn't terminate naturally — caller (e.g. the
                # delegate adapter in demo/runner.py) inspects stop_reason
                # and reports the failure. Returning a synthetic AgentResult
                # rather than raising lets the caller decide policy.
                if self.verbose:
                    print(f"[{self.name}] hit max_turns={self.config.max_turns}; "
                          f"exiting without natural termination")
                return AgentResult(
                    final_text=f"[agent={self.name} hit max_turns="
                                f"{self.config.max_turns} without termination]",
                    stop_reason="max_turns",
                    turns=turns - 1,
                    tool_calls=tool_calls,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_create,
                )
            kwargs: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "system": self.system,
                "tools": [t.to_dict() for t in self.tools],
                "messages": self.messages,
            }
            # Best-effort terminator nudge in the final 2 turns: force the
            # model to use SOME tool. We use {type: any} (broadly supported)
            # rather than {type: tool, name: ...}, which DeepSeek's reasoning
            # models reject with 400. Once a provider rejects tool_choice
            # in this run, we latch and stop sending it — saves the
            # wasted-retry round-trip on subsequent turns. max_turns is
            # the actual safety net.
            if (self.force_terminator
                    and turns >= self.config.max_turns - 1
                    and not self._tool_choice_unsupported):
                kwargs["tool_choice"] = {"type": "any"}

            try:
                resp = self.client.messages.create(**kwargs)
            except Exception as e:
                # Defensive: if the provider rejects tool_choice, retry
                # once without it AND latch so we don't try again this
                # run. Surfacing the original error otherwise keeps
                # auth/rate-limit failures loud.
                if "tool_choice" in kwargs and "tool_choice" in str(e):
                    self._tool_choice_unsupported = True
                    kwargs.pop("tool_choice")
                    resp = self.client.messages.create(**kwargs)
                else:
                    raise
            in_tok += resp.usage.input_tokens
            out_tok += resp.usage.output_tokens
            # DeepSeek (and Anthropic-native, if ever used) expose these
            # on usage when prompt caching is in play. They may be missing
            # or None on other providers (Qwen) — getattr-with-default
            # keeps us robust.
            cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_create += getattr(
                resp.usage, "cache_creation_input_tokens", 0) or 0

            self.messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if self.verbose:
                for b in resp.content:
                    if b.type == "text":
                        print(f"[{self.name}] {b.text}")
                    elif b.type == "tool_use":
                        print(f"[{self.name}] -> {b.name}({b.input})")

            if resp.stop_reason != "tool_use" or not tool_uses:
                final_text = "".join(b.text for b in resp.content if b.type == "text")
                return AgentResult(
                    final_text=final_text,
                    stop_reason=resp.stop_reason,
                    turns=turns,
                    tool_calls=tool_calls,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_create,
                )

            tool_results = [self._exec_tool(c) for c in tool_uses]
            tool_calls += len(tool_results)
            self.messages.append({"role": "user", "content": tool_results})
