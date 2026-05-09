"""Demo entrypoint: supervisor runs a workload, mints a grant, delegates to a
subagent. Replay-mode by default — uses fixtures from eval/fixtures/.

Loads `.env` at the project root before constructing the Agent so users can
keep `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` out of their shell rc.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from artifactstore import ArtifactStore
from demo.agent import Agent, ModelConfig
from demo.prompts import SUBAGENT_SYSTEM, SUPERVISOR_SYSTEM
from demo.tools import subagent_tools, supervisor_tools
from demo.workloads import ViewPolicy

# Search ancestors of the runner module up to a sensible cap; this lets
# `python -m demo.runner` work whether invoked from the project root or a
# subdirectory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Minimal `.env` loader. Sets KEY=VALUE pairs from `path` into os.environ
    if not already set (or always, if override=True). Strips matching surrounding
    quotes. Returns the dict of keys actually set, for tests/observability.

    Format supported: `KEY=value` per line, '#' comments, blank lines. Surrounding
    single or double quotes on the value are stripped. No multi-line values, no
    interpolation, no `export ` prefix. If you need more, switch to python-dotenv.
    """
    target = path if path is not None else (_PROJECT_ROOT / ".env")
    if not target.is_file():
        return {}
    set_keys: dict[str, str] = {}
    for raw in target.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if not key:
            continue
        # Treat empty existing values as unset so `export ANTHROPIC_API_KEY=`
        # in the user's shell rc doesn't shadow a real .env value. This is
        # what python-dotenv does too.
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        set_keys[key] = value
    return set_keys


def _extract_submit_report(messages: list[dict]) -> dict | None:
    """Walk an agent's message history and return the most recent
    submit_report tool_use input. Returns None if the subagent never called it.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        for b in reversed(m.get("content", []) or []):
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "submit_report":
                return dict(b.input)
    return None


def _make_run_subagent(store: ArtifactStore, model: str, verbose: bool,
                       client: Any | None = None):
    """Returns a `run_subagent(task, grant_id) -> dict` callable used as the
    supervisor's `delegate` adapter. `client` lets tests inject a stub Anthropic
    client; production passes None and the Agent constructs a real one.
    """
    def run_subagent(task: str, grant_id: str) -> dict:
        sub = Agent(
            name="subagent",
            system=SUBAGENT_SYSTEM,
            tools=subagent_tools(store, grant_id),
            config=ModelConfig(model=model),
            client=client,
            verbose=verbose,
            force_terminator="submit_report",
        )
        r = sub.run(task)
        submit = _extract_submit_report(sub.messages) or {}
        submitted = bool(submit)
        # Surface delegation failure clearly so the supervisor's prompt has a
        # signal to act on. PLAN §20.2 explicitly says: unresolvable citation
        # = report rejected — same applies to "no submission at all".
        error: str | None = None
        if not submitted:
            error = (
                f"subagent did not call submit_report. "
                f"stop_reason={r.stop_reason}, turns={r.turns}, "
                f"tool_calls={r.tool_calls}. "
                f"This usually means the subagent ran out of turns "
                f"(max_turns) without committing to a diagnosis."
            )
        return {
            "submitted":   submitted,
            "error":       error,
            # The diagnosis + citations the subagent submitted. These are what
            # the supervisor verifies — `final_text` (post-submit chatter) is
            # only kept around for diagnostics.
            "diagnosis":   submit.get("diagnosis"),
            "citations":   submit.get("citations", []),
            "confidence":  submit.get("confidence"),
            "final_text":  r.final_text,
            "stop_reason": r.stop_reason,
            "turns":       r.turns,
            "tool_calls":  r.tool_calls,
            "input_tokens":  r.input_tokens,
            "output_tokens": r.output_tokens,
            "audit": store.audit(grant_id),
        }
    return run_subagent


def demo(*, db: str, kind: str, target: str, model: str,
         policy: ViewPolicy, verbose: bool, base_url: str | None = None) -> None:
    store = ArtifactStore.init(db)

    # Echo the resolved provider config before we make any paid call. The user
    # can Ctrl-C if it's not what they expected — saves spend on misconfig.
    resolved_base = base_url or os.environ.get("ANTHROPIC_BASE_URL") or "(SDK default — Anthropic)"
    has_key = "yes" if os.environ.get("ANTHROPIC_API_KEY") else "NO (will fail)"
    print(f"[runner] model={model} base_url={resolved_base} api_key={has_key}")

    sup = Agent(
        name="supervisor",
        system=SUPERVISOR_SYSTEM,
        tools=supervisor_tools(
            store,
            session_id="demo",
            issuer_agent_id="supervisor",
            run_subagent=_make_run_subagent(store, model, verbose),
            policy=policy,
        ),
        config=ModelConfig(model=model, base_url=base_url),
        verbose=verbose,
    )

    task = (
        f"Run the {kind} workload on target '{target}'. Identify the root cause "
        f"of any failure. Delegate the diagnosis to a subagent under the "
        f"narrowest grant possible. Verify every citation in the subagent's "
        f"report by calling expand_artifact. Produce a final answer."
    )
    result = sup.run(task)

    print("\n=== supervisor final ===")
    print(result.final_text)
    print(f"\nturns={result.turns} tool_calls={result.tool_calls} "
          f"in={result.input_tokens} out={result.output_tokens}")


def _verify_tool_use(model: str, base_url: str | None) -> int:
    """One-shot LIVE call (~1 cent) that confirms the configured provider
    emits real Anthropic-format `tool_use` blocks, not OpenAI-style
    `function_call` shapes. Required before trusting the live demo or the
    eval driver — DeepSeek's /anthropic endpoint claims compatibility, but
    their docs only show OpenAI examples for tool use.

    Pass criteria:
      - the SDK round-trip succeeds (no parse errors)
      - the model emits at least one `type=tool_use` block
      - our local tool runs (proves the loop wired up correctly)
    """
    from demo.agent import Agent, ModelConfig, Tool

    calls: list[str] = []

    def echo(text: str) -> str:
        calls.append(text)
        return f"ok: {text}"

    tool = Tool(
        name="echo",
        description="Echo a short message back. Call this exactly once.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=echo,
    )

    agent = Agent(
        name="probe",
        system=("You are a tool-use probe. Call the echo tool exactly once "
                "with the text 'hello'. After the tool returns, reply "
                "'done.' and stop."),
        tools=[tool],
        config=ModelConfig(model=model, max_tokens=200, base_url=base_url),
    )
    print(f"[verify] sending one paid request to {model} ...")
    try:
        result = agent.run("Please call the echo tool now.")
    except Exception as e:  # noqa: BLE001 — surface to the user
        print(f"FAIL  exception: {type(e).__name__}: {e}")
        return 2

    if not calls:
        print(f"FAIL  model never called the echo tool.")
        print(f"      stop_reason={result.stop_reason} turns={result.turns} "
              f"tool_calls={result.tool_calls}")
        print(f"      This usually means the endpoint emitted a function_call "
              f"shape (OpenAI-style) instead of Anthropic tool_use blocks. "
              f"Switch providers or use the OpenAI-compatible endpoint with "
              f"a different client.")
        return 2

    print(f"PASS  model called echo({calls[0]!r})")
    print(f"      turns={result.turns} tool_calls={result.tool_calls} "
          f"in={result.input_tokens} out={result.output_tokens} tokens")
    print(f"      DeepSeek/Anthropic-shape tool_use is wired up correctly.")
    return 0


def _verify_tool_choice(model: str, base_url: str | None) -> int:
    """Probe whether the provider honors `tool_choice: {type:'tool', name:...}`.

    Why: the live demo relies on `force_terminator` which sets tool_choice to
    submit_report after N turns. If the provider silently ignores tool_choice,
    the subagent never submits and the eval citation chain breaks. The first
    live demo run showed exactly this on DeepSeek; this probe pins down whether
    tool_choice is the issue.

    Method: define two tools (`hello` and `goodbye`). Ask the model to call
    `hello`. Force tool_choice=goodbye. If the response calls `goodbye`,
    tool_choice works. If it calls `hello`, tool_choice is ignored.
    """
    from anthropic import Anthropic
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    tools = [
        {"name": "hello",
         "description": "Say hello. Call this when greeting.",
         "input_schema": schema},
        {"name": "goodbye",
         "description": "Say goodbye. Call this when leaving.",
         "input_schema": schema},
    ]
    client_kwargs = {}
    if base_url or os.environ.get("ANTHROPIC_BASE_URL"):
        client_kwargs["base_url"] = base_url or os.environ["ANTHROPIC_BASE_URL"]
    client = Anthropic(**client_kwargs)
    print(f"[probe] {model} via {client.base_url} — forcing tool_choice=goodbye"
          f" while asking for hello ...")
    # Try named tool_choice first; if rejected, fall back to "any".
    for choice, label in [
        ({"type": "tool", "name": "goodbye"}, "tool_choice={tool,goodbye}"),
        ({"type": "any"}, "tool_choice={any}"),
    ]:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=200,
                system="You are a tool-use probe.",
                tools=tools,
                tool_choice=choice,
                messages=[{"role": "user", "content":
                           "Please call the hello tool with text='hi'."}],
            )
            print(f"[probe] {label} accepted")
            break
        except Exception as e:  # noqa: BLE001
            print(f"[probe] {label} rejected: {type(e).__name__}: {e}")
            resp = None
    if resp is None:
        print(f"FAIL  no tool_choice variant accepted")
        return 2

    tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not tool_uses:
        print(f"FAIL  no tool_use block emitted. stop_reason={resp.stop_reason}")
        return 2
    chosen = tool_uses[0].name
    if chosen == "goodbye":
        print(f"PASS  model called {chosen!r} as forced — tool_choice IS honored.")
        print(f"      We can rely on force_terminator at low turn thresholds.")
        return 0
    if chosen == "hello":
        print(f"FAIL  model called {chosen!r} despite tool_choice=goodbye — "
              f"provider IGNORES tool_choice.")
        print(f"      We must use prompting + max_turns cap, not force_terminator.")
        return 1
    print(f"WEIRD model called {chosen!r} (neither tool). stop_reason="
          f"{resp.stop_reason}")
    return 2


def _check_config(model: str, base_url: str | None) -> int:
    """Print resolved provider config and construct (but don't call) an Agent.
    No network. Returns an exit code — 0 if the SDK accepted the config, 2 if
    something's missing. Use this before kicking off a paid run."""
    from demo.agent import DEEPSEEK_BASE_URL, Agent, ModelConfig, Tool
    resolved_base = (base_url
                     or os.environ.get("ANTHROPIC_BASE_URL")
                     or "(SDK default — Anthropic)")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"model       {model}")
    print(f"base_url    {resolved_base}")
    print(f"api_key     {'set' if has_key else 'MISSING — runner will fail'}")
    expected_provider = ("DeepSeek" if "deepseek" in str(resolved_base)
                         else "Anthropic")
    print(f"provider    {expected_provider}")
    if not has_key:
        return 2
    # Construct the agent so the SDK validates the inputs. No request is sent.
    try:
        a = Agent(name="probe", system="probe", tools=[],
                  config=ModelConfig(model=model, base_url=base_url))
    except Exception as e:
        print(f"FAIL        {type(e).__name__}: {e}")
        return 2
    print(f"client      {type(a.client).__name__} @ {a.client.base_url}")
    print("OK — env wired up. No request was made; no tokens spent.")
    return 0


def main() -> None:
    from demo.agent import DEFAULT_MODEL
    # Load .env BEFORE we touch ModelConfig / Agent. We use override=True so
    # values in the project-local .env take precedence over global shell
    # exports — common gotcha: a shell rc with ANTHROPIC_BASE_URL pointing at
    # Anthropic shadows the project's DeepSeek base_url otherwise. The
    # project file is the explicit, version-able config; the shell isn't.
    load_dotenv(override=True)
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="demo.db")
    p.add_argument("--kind", default="pytest")
    p.add_argument("--target", default="auth_expiry")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="Model id (default: deepseek-v4-pro). Use any model "
                        "the configured provider supports.")
    p.add_argument("--base-url", default=None,
                   help="Override provider endpoint (else ANTHROPIC_BASE_URL "
                        "env var, else native Anthropic). For DeepSeek: "
                        "https://api.deepseek.com/anthropic")
    p.add_argument("--policy",
                   choices=[v.value for v in ViewPolicy],
                   default=ViewPolicy.ARTIFACT.value)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--check-config", action="store_true",
                   help="Print resolved provider config and exit. Makes no "
                        "API calls — use this to verify .env wiring before "
                        "spending tokens.")
    p.add_argument("--verify-tool-use", action="store_true",
                   help="Make ONE small live API call to confirm the provider "
                        "emits Anthropic-format tool_use blocks. Costs ~1 cent. "
                        "Required before trusting the eval driver.")
    p.add_argument("--verify-tool-choice", action="store_true",
                   help="Probe whether the provider honors tool_choice. "
                        "If False, we cannot rely on force_terminator and "
                        "must lean on max_turns + prompting instead.")
    args = p.parse_args()
    if args.check_config:
        raise SystemExit(_check_config(args.model, args.base_url))
    if args.verify_tool_use:
        raise SystemExit(_verify_tool_use(args.model, args.base_url))
    if args.verify_tool_choice:
        raise SystemExit(_verify_tool_choice(args.model, args.base_url))
    demo(db=args.db, kind=args.kind, target=args.target,
         model=args.model, policy=ViewPolicy(args.policy),
         verbose=args.verbose, base_url=args.base_url)


if __name__ == "__main__":
    main()
