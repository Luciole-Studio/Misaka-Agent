"""Agent runtime package."""

import importlib

_EXPORTS = {
    "ProxyAssistantMessageEvent": ("misaka.agent.proxy", "ProxyAssistantMessageEvent"),
    "ProxyStreamOptions": ("misaka.agent.proxy", "ProxyStreamOptions"),
    "getDefaultStreamFn": ("misaka.agent.stream_fn", "getDefaultStreamFn"),
    "get_default_stream_fn": ("misaka.agent.stream_fn", "get_default_stream_fn"),
    "setDefaultStreamFn": ("misaka.agent.stream_fn", "setDefaultStreamFn"),
    "set_default_stream_fn": ("misaka.agent.stream_fn", "set_default_stream_fn"),
    "streamProxy": ("misaka.agent.proxy", "streamProxy"),
    "stream_proxy": ("misaka.agent.proxy", "stream_proxy"),
}


def __getattr__(name: str):
    entry = _EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, source = entry
    value = getattr(importlib.import_module(module), source)
    globals()[name] = value
    return value


__all__ = [
    "ProxyAssistantMessageEvent",
    "ProxyStreamOptions",
    "getDefaultStreamFn",
    "get_default_stream_fn",
    "setDefaultStreamFn",
    "set_default_stream_fn",
    "streamProxy",
    "stream_proxy",
]
