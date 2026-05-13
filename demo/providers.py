"""Provider resolution: map a model name to the (api_key, base_url) pair
that should be used for it. Lets the eval sweep run against multiple
model families without editing .env between runs.

The agent loop uses the Anthropic SDK as its HTTP client, so every
provider must expose an Anthropic-Messages-API-compatible endpoint.
Picking by *model-name prefix* keeps the call sites trivial: pass
`--model deepseek-v4-pro` and you get DeepSeek; pass `--model qwen3.6-max`
and you get Qwen; pass `--model claude-sonnet-4-5` and you get native
Anthropic. No global flag, no .env swap, no manual base_url juggling.

Each provider has its own env vars. There is no cross-provider
fallback for either keys or URLs — that would silently authenticate
a request with the wrong credentials and only surface as a confusing
401 at the API call. A missing key raises `ProviderError` up-front;
a missing URL falls through to the provider's hard-coded default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """One provider's wiring. `key_env` is the env var the resolver
    reads — no cross-provider fallback. `default_base_url` can be
    overridden by `base_url_env` if the user needs to point at a
    self-hosted or alternate-region endpoint."""
    name: str
    key_env: str
    base_url_env: str
    default_base_url: str | None  # None means use the SDK's built-in default


# Registry. Add a new provider here + a prefix entry in `_PREFIXES` and
# the rest of the codebase picks it up.
PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        name="DeepSeek",
        key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com/anthropic",
    ),
    "qwen": Provider(
        name="Qwen (Alibaba Model Studio)",
        key_env="QWEN_API_KEY",
        base_url_env="QWEN_BASE_URL",
        # International tenant; mainland China users override via QWEN_BASE_URL.
        # Endpoint path varies by account tier — verify with --check-config /
        # --verify-tool-use before running paid sweeps.
        default_base_url="https://dashscope-intl.aliyuncs.com/api/v2/apps/anthropic",
    ),
    "anthropic": Provider(
        name="Anthropic",
        key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_BASE_URL",
        default_base_url=None,  # SDK default = api.anthropic.com
    ),
}


# Prefix → provider key. Order matters only insofar as the longest-match
# wins; we keep these mutually exclusive so a flat dict is fine.
_PREFIXES: dict[str, str] = {
    "deepseek": "deepseek",
    "qwen": "qwen",
    "claude": "anthropic",
}


class ProviderError(RuntimeError):
    """Raised when we can't resolve a provider or the resolved provider
    has no key set. Carries enough context for the runner to print a
    pointed error message instead of letting the SDK 401."""


def _detect(model: str) -> str:
    """Return the registry key for a model name. Falls back to 'anthropic'
    so an unknown name still hits the legacy ANTHROPIC_API_KEY path."""
    m = model.lower().lstrip()
    for prefix, key in _PREFIXES.items():
        if m.startswith(prefix):
            return key
    return "anthropic"


def resolve(model: str) -> tuple[str, str | None, Provider]:
    """Return (api_key, base_url_or_None, Provider) for `model`.

    Lookup order for the API key:
      1. Provider-specific env var (e.g. DEEPSEEK_API_KEY for deepseek-*).
      2. Legacy ANTHROPIC_API_KEY (so an existing single-provider .env
         continues to work; a clear failure mode if it points at the
         wrong provider's key).

    Lookup order for the base URL:
      1. Provider-specific env var (DEEPSEEK_BASE_URL, etc.).
      2. The provider's hard-coded default.
      3. For 'anthropic', this can be None (SDK uses api.anthropic.com).

    Raises ProviderError with a pointed message if no key resolves.
    """
    provider_key = _detect(model)
    p = PROVIDERS[provider_key]

    # Provider-specific key, full stop. No ANTHROPIC_API_KEY fallback —
    # that would silently authenticate a Qwen / DeepSeek request with
    # the wrong key and only surface as a confusing 401 at the actual
    # API call. Symmetric with the base-URL behaviour.
    api_key = os.environ.get(p.key_env)
    if not api_key:
        raise ProviderError(
            f"No API key found for provider {p.name!r} (model={model!r}). "
            f"Set {p.key_env}=... in .env. (Each provider uses its own "
            f"env var so a sweep can run multiple model families without "
            f"silently misrouting credentials.)"
        )

    # Base-URL lookup order:
    #   1. Provider-specific env var (DEEPSEEK_BASE_URL / QWEN_BASE_URL /
    #      ANTHROPIC_BASE_URL — the provider's *own* env var).
    #   2. The provider's hard-coded default.
    # Crucially we do *not* fall back across providers. A legacy
    # ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic must not
    # silently route a `--model qwen3.6-max` request to DeepSeek; the
    # caller would never know. For each provider, the only acceptable
    # URL overrides come from that provider's own env var.
    base_url = os.environ.get(p.base_url_env) or p.default_base_url
    return api_key, base_url, p


def describe(model: str) -> dict[str, object]:
    """Resolve and return a dict useful for --check-config / manifest.json
    output. Never raises — masks the key and reports 'MISSING' if absent."""
    provider_key = _detect(model)
    p = PROVIDERS[provider_key]
    raw_key = os.environ.get(p.key_env)
    base_url = os.environ.get(p.base_url_env) or p.default_base_url
    return {
        "model": model,
        "provider": p.name,
        "key_env": p.key_env,
        "key_present": bool(raw_key),
        "base_url": base_url or "(SDK default — api.anthropic.com)",
    }
