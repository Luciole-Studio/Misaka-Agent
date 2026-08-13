"""RPC mode exports."""

from .jsonl import (
    JsonlLineBuffer,
    attach_jsonl_line_reader,
    attachJsonlLineReader,
    iter_jsonl_lines,
    serialize_json_line,
    serializeJsonLine,
)
from .rpc_client import RpcClient, RpcClientOptions
from .rpc_mode import run_rpc_mode, runRpcMode
from .rpc_types import (
    RpcCommand,
    RpcExtensionUIRequest,
    RpcExtensionUIResponse,
    RpcResponse,
    RpcSessionState,
    RpcSlashCommand,
)

__all__ = [
    "JsonlLineBuffer",
    "RpcClient",
    "RpcClientOptions",
    "RpcCommand",
    "RpcExtensionUIRequest",
    "RpcExtensionUIResponse",
    "RpcResponse",
    "RpcSessionState",
    "RpcSlashCommand",
    "attachJsonlLineReader",
    "attach_jsonl_line_reader",
    "iter_jsonl_lines",
    "runRpcMode",
    "run_rpc_mode",
    "serializeJsonLine",
    "serialize_json_line",
]
