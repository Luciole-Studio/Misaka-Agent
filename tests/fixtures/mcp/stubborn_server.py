"""pi's ``packages/mcp/test/fixtures/stubborn-server.mjs``: answers initialize, spawns a grandchild that
outlives stdin, and ignores stdin EOF and SIGTERM (POSIX only)."""

import json
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(f"grandchild {grandchild.pid}", file=sys.stderr, flush=True)

for line in sys.stdin.buffer:
    message = json.loads(line)
    if message.get("method") != "initialize":
        continue
    result = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "serverInfo": {"name": "stubborn-fixture", "version": "1.0.0"},
    }
    sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode() + b"\n")
    sys.stdout.buffer.flush()

while True:
    time.sleep(1)
