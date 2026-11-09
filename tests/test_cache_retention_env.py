"""One name turns on long prompt-cache retention for every provider and for cache warming.

Upstream reads ``PI_CACHE_RETENTION`` everywhere. The shared provider helper here read
``MISAKA_CACHE_RETENTION`` while the pi-messages provider and the cache warmer read
``PI_CACHE_RETENTION``, so either name worked for only part of the requests.
"""
from types import SimpleNamespace

from misaka.ai.api import openai_responses, pi_messages
from misaka.core.cache_warmer import getPromptCacheTtlMs


def test_request_env_selects_long_retention_everywhere():
    env = {"PI_CACHE_RETENTION": "long"}
    assert pi_messages._resolveCacheRetention(None, env) == "long"
    model = SimpleNamespace(promptCache={"short": 300, "long": 3600})
    assert getPromptCacheTtlMs(model, {"env": env}) == 3_600_000


def test_process_env_selects_long_retention_everywhere(monkeypatch):
    monkeypatch.setenv("PI_CACHE_RETENTION", "long")
    assert pi_messages._resolveCacheRetention(None, None) == "long"


def test_the_old_name_is_not_read(monkeypatch):
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)
    monkeypatch.setenv("MISAKA_CACHE_RETENTION", "long")
    assert pi_messages._resolveCacheRetention(None, None) is None
    assert openai_responses._resolveCacheRetention(None, None) == "short"
