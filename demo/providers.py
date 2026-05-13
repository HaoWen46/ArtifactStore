"""Provider resolution: map a model name to the (api_key, base_url) pair
that should be used for it. Lets the eval sweep run multiple model
families back-to-back without editing .env between runs.

The agent loop uses the Anthropic Python SDK as its HTTP client, so every
provider must expose an Anthropic-Messages-API-compatible endpoint.
Picking by *model-name prefix* keeps the call sites trivial:
  --model deepseek-v4-pro    -> DeepSeek
  --model qwen3.6-plus       -> Qwen (Alibaba Model Studio)
No global flag, no .env swap, no manual base_url juggling.

Each provider reads ONLY its own env vars. There is no cross-provider
fallback for either keys or URLs — that would silently authenticate a
request with the wrong credentials and only surface as a confusing 401
at the API call. A missing key raises `ProviderError` up-front.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """One provider's wiring. `key_env` is the env var the resolver
    reads — no cross-provider fallback. `default_base_url` can be
    overridden by `base_url_env` for self-hosted / regional endpoints."""
    name: str
    key_env: str
    base_url_env: str
    default_base_url: str


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
        # International tenant Anthropic-compatible endpoint. Mainland-CN
        # tenants override via QWEN_BASE_URL. Verified against:
        # https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages
        default_base_url="https://dashscope-intl.aliyuncs.com/apps/anthropic",
    ),
}


# Prefix → provider key. Longest match wins; the entries below are
# mutually exclusive so a flat dict is enough.
_PREFIXES: dict[str, str] = {
    "deepseek": "deepseek",
    "qwen": "qwen",
}


class ProviderError(RuntimeError):
    """Raised when we can't resolve a provider or the resolved provider
    has no key set. Carries enough context for the runner to print a
    pointed error message instead of letting the SDK 401."""


def _detect(model: str) -> str:
    """Return the registry key for a model name. Raises ProviderError if
    the model has no recognised provider prefix — no silent fallback."""
    m = model.lower().lstrip()
    for prefix, key in _PREFIXES.items():
        if m.startswith(prefix):
            return key
    raise ProviderError(
        f"No provider matches model={model!r}. Supported prefixes: "
        f"{sorted(_PREFIXES)}. (deepseek-* for DeepSeek, qwen* for "
        f"Alibaba Model Studio.)"
    )


def resolve(model: str) -> tuple[str, str, Provider]:
    """Return (api_key, base_url, Provider) for `model`.

    API key lookup:
      1. Provider-specific env var (DEEPSEEK_API_KEY / QWEN_API_KEY).
      No cross-provider fallback — symmetric with base-URL behaviour.

    Base URL lookup:
      1. Provider-specific env var (DEEPSEEK_BASE_URL / QWEN_BASE_URL).
      2. Provider's hard-coded default.

    Raises ProviderError with a pointed message if no key resolves.
    """
    provider_key = _detect(model)
    p = PROVIDERS[provider_key]
    api_key = os.environ.get(p.key_env)
    if not api_key:
        raise ProviderError(
            f"No API key found for provider {p.name!r} (model={model!r}). "
            f"Set {p.key_env}=... in .env. (Each provider uses its own "
            f"env var so a sweep can run multiple model families without "
            f"silently misrouting credentials.)"
        )
    base_url = os.environ.get(p.base_url_env) or p.default_base_url
    return api_key, base_url, p


def describe(model: str) -> dict[str, object]:
    """Resolve and return a dict useful for --check-config / manifest.json
    output. Never raises — masks the key and reports 'MISSING' if absent,
    and reports 'UNKNOWN' provider for an unrecognised prefix."""
    try:
        provider_key = _detect(model)
    except ProviderError as exc:
        return {
            "model": model,
            "provider": "UNKNOWN",
            "key_env": "(none)",
            "key_present": False,
            "base_url": "(no provider matched)",
            "error": str(exc),
        }
    p = PROVIDERS[provider_key]
    raw_key = os.environ.get(p.key_env)
    base_url = os.environ.get(p.base_url_env) or p.default_base_url
    return {
        "model": model,
        "provider": p.name,
        "key_env": p.key_env,
        "key_present": bool(raw_key),
        "base_url": base_url,
    }
