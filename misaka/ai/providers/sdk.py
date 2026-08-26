"""Provider SDKs are optional extras. A provider module imports its SDK at import time but
must not fail when the extra is missing: the client factory calls ``require`` instead, so
the error names the extra to install rather than a bare ImportError at startup."""

EXTRAS = {"anthropic": "anthropic", "openai": "openai", "google-genai": "google", "boto3": "bedrock", "mistralai": "mistral"}


def require(sdk, package):
    """``sdk`` is the imported object (None when its import failed); ``package`` the PyPI name."""
    if sdk is None:
        extra = EXTRAS[package]
        raise RuntimeError(f"The {package} package is not installed. Install it with "
                           f"`pip install 'misaka[{extra}]'` (or `uv sync --extra {extra}`).")
    return sdk
