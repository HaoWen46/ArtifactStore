"""Minimal Claude tool-use agent loop.

Adapted from anthropic-quickstarts/agents (MIT). Stripped down: no MCP,
no async-everywhere. Tools are sync callables; we wrap them in to_thread
only if a tool ever needs it. The loop is the canonical one from
platform.claude.com/docs/.../how-tool-use-works.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from anthropic import Anthropic


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
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 4096
    temperature: float = 1.0


@dataclass
class AgentResult:
    final_text: str
    stop_reason: str
    turns: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


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
        self.client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.verbose = verbose
        self.force_terminator = force_terminator
        self._tool_dict = {t.name: t for t in tools}
        self.messages: list[dict[str, Any]] = []

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
        in_tok = out_tok = turns = tool_calls = 0

        while True:
            turns += 1
            kwargs: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "system": self.system,
                "tools": [t.to_dict() for t in self.tools],
                "messages": self.messages,
            }
            if self.force_terminator and turns >= 8:
                kwargs["tool_choice"] = {"type": "tool", "name": self.force_terminator}

            resp = self.client.messages.create(**kwargs)
            in_tok += resp.usage.input_tokens
            out_tok += resp.usage.output_tokens

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
                )

            tool_results = [self._exec_tool(c) for c in tool_uses]
            tool_calls += len(tool_results)
            self.messages.append({"role": "user", "content": tool_results})
