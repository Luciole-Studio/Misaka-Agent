"""xAI (Grok) provider profile."""

from ..host.profiles import HOST_VERSION as _HERMES_VERSION
from ..host.profiles import register_provider
from .provider_profile import ProviderProfile

xai = ProviderProfile(
    name="xai", aliases=("grok", "x-ai", "x.ai"), api_mode="codex_responses", env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1", auth_type="api_key",
    default_headers={"User-Agent": f"Hermes-Agent/{_HERMES_VERSION}"},
)

register_provider(xai)
