"""The plugin's two Hermes imports, backed by pinned source rather than an ABC stub.

Only the fallback class is lazy: starting MISAKA must not import a full compressor.
The implementation's dependencies stay package-scoped, never in an external Hermes.
"""
import sys
from types import ModuleType

from misaka.extensions.hermes_lcm.native import context_engine

ContextEngine = context_engine.ContextEngine


def _native_compressor(name):
    if name != "ContextCompressor":
        raise AttributeError(name)
    from misaka.extensions.hermes_lcm.native.context_compressor import ContextCompressor
    return ContextCompressor


_agent = sys.modules.get("agent")
if _agent is None:
    _agent = ModuleType("agent")
    _agent.__path__ = []
    sys.modules["agent"] = _agent

sys.modules["agent.context_engine"] = context_engine
_agent.context_engine = context_engine
_compressor = ModuleType("agent.context_compressor")
_compressor.__getattr__ = _native_compressor
sys.modules["agent.context_compressor"] = _compressor
_agent.context_compressor = _compressor
