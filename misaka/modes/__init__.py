"""Shared mode exports for coding-agent."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_PUBLIC_EXPORTS: dict[str, tuple[str, str]] = {
    "InteractiveMode": ("misaka.modes.interactive.interactive_mode", "InteractiveMode"),
    "InteractiveModeOptions": ("misaka.modes.interactive.interactive_mode", "InteractiveModeOptions"),
    "PrintModeOptions": ("misaka.modes.print_mode", "PrintModeOptions"),
    "runPrintMode": ("misaka.modes.print_mode", "runPrintMode"),
    "ModelInfo": ("misaka.modes.rpc.rpc_client", "ModelInfo"),
    "RpcClient": ("misaka.modes.rpc.rpc_client", "RpcClient"),
    "RpcClientOptions": ("misaka.modes.rpc.rpc_client", "RpcClientOptions"),
    "RpcEventListener": ("misaka.modes.rpc.rpc_client", "RpcEventListener"),
    "runRpcMode": ("misaka.modes.rpc.rpc_mode", "runRpcMode"),
    "RpcCommand": ("misaka.modes.rpc.rpc_types", "RpcCommand"),
    "RpcResponse": ("misaka.modes.rpc.rpc_types", "RpcResponse"),
    "RpcSessionState": ("misaka.modes.rpc.rpc_types", "RpcSessionState"),
}

__all__ = list(_PUBLIC_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _PUBLIC_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    module = import_module(module_name)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
