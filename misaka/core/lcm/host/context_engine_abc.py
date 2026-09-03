"""Stand in for Hermes' ``agent.context_engine`` so the vendored engine imports unchanged.

``vendor/engine.py`` line 21 is ``from agent.context_engine import ContextEngine`` and
line 363 puts that class last in ``LCMEngine``'s bases. That single line is the port's
only import-time coupling to the Hermes host, and rewriting it would be a permanent
edit to the file an upstream resync diffs against - so instead this module registers a
faithful ``ContextEngine`` under the name upstream expects, and ``vendor/__init__.py``
imports this module before anything can import ``vendor.engine``.

What "faithful" means here is the surface ``LCMEngine`` actually consumes, not all 489
lines of the real ABC:

* the four abstract methods it overrides (``name``, ``update_from_response``,
  ``should_compress``, ``compress``) - abstract, so a broken subclass still fails loudly;
* the ten token/compaction class attributes the protocol says engines must maintain
  (``LCMEngine.__init__`` assigns every one, so these defaults only cover a
  half-constructed engine, but they are what ``get_status`` below reads);
* the concrete hooks ``LCMEngine`` inherits rather than overrides
  (``should_compress_info``, ``prune_tool_results_only``, ``select_context``,
  ``on_turn_complete``, ``should_defer_preflight_to_real_usage``,
  ``has_content_to_compress``, ``get_automatic_compaction_status_message``);
* and the two it overrides *and calls through* - ``on_session_reset`` (engine.py:3449)
  and ``get_status`` (engine.py:3752) - whose bodies are therefore load-bearing.

Deliberately absent: ``on_session_start`` / ``on_session_end`` / ``get_tool_schemas`` /
``handle_tool_call`` (overridden with no ``super()`` call), ``update_model`` (likewise,
and the real one imports ``agent.context_compressor``), and the module's helpers
``sanitize_memory_context`` / ``automatic_compaction_status_message`` /
``MEMORY_CONTEXT_MAX_CHARS``, which nothing in the vendored closure names.
"""

import sys
from abc import ABC, abstractmethod
from types import ModuleType
from typing import Any


class ContextEngine(ABC):
    """The Hermes context-engine protocol, reduced to what the vendored engine uses."""

    # Token state the host reads directly off the engine.
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    # Compaction parameters. protect_first_n counts non-system head messages kept
    # verbatim; the system prompt is protected implicitly on top of them.
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    # False keeps successful automatic compaction silent; warnings and explicit
    # commands still surface.
    emit_automatic_compaction_status: bool = True

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""

    @abstractmethod
    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Update tracked token usage from a normalized API usage dict."""

    @abstractmethod
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Return True if compaction should fire this turn."""

    @abstractmethod
    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """Compact the message list and return the new one."""

    def should_compress_info(self, prompt_tokens: int | None = None) -> tuple[bool, str | None]:
        """``(should_compress, reason)``; the reason is None unless an engine supplies one."""
        return self.should_compress(prompt_tokens), None

    def prune_tool_results_only(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Cheap tool-result trim. Default no-op: ``(messages, 0)``."""
        return messages, 0

    def select_context(
        self,
        request_messages: list[dict[str, Any]],
        *,
        conversation_messages: list[dict[str, Any]] | None = None,
        incoming_message: dict[str, Any] | None = None,
        budget_tokens: int = 0,
    ) -> list[dict[str, Any]]:
        """Replace the context for this one request. Default None: leave it alone."""
        return None

    def on_turn_complete(
        self,
        messages: list[dict[str, Any]],
        usage: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Observe a finished turn. Default no-op."""

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Rough pre-API check. Default False: skip preflight."""
        return False

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """Whether preflight should trust recent real usage instead. Default False."""
        return False

    def get_automatic_compaction_status_message(
        self,
        *,
        phase: str,
        default_message: str,
        **context: Any,
    ) -> str | None:
        """Host-visible status for an automatic compaction event; None suppresses it."""
        if not self.emit_automatic_compaction_status:
            return None
        return default_message

    def has_content_to_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Preflight guard for a manual compress. Default True: always attempt."""
        return True

    def on_session_reset(self) -> None:
        """Reset per-session token state. Called through ``super()`` by the engine."""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def get_status(self) -> dict[str, Any]:
        """Status dict the engine extends. Called through ``super()`` by the engine."""
        # -1 is the "compression just ran, real usage pending" sentinel; clamp it so
        # readers never see a negative usage_percent on the transitional turn.
        last_prompt = max(0, self.last_prompt_tokens)
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, last_prompt / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }


# Install the seam. setdefault throughout: on a host that really does ship `agent`,
# the real module wins and this stub stays unused.
_agent = sys.modules.get("agent")
if _agent is None:
    _agent = ModuleType("agent")
    _agent.__path__ = []
    sys.modules["agent"] = _agent

if "agent.context_engine" not in sys.modules:
    _module = ModuleType("agent.context_engine")
    _module.ContextEngine = ContextEngine
    sys.modules["agent.context_engine"] = _module
    _agent.context_engine = _module
