"""pi env's ``test/old-daemon.ts``: a stand-in for a daemon released before ``hello.features``: it answers ``hello``
without ``features`` and refuses every other operation as an unknown one, as such a daemon refuses ``listFiles``.
Started as ``python old_env_daemon.py serve --token <hex>`` (``tests.env_support.oldDaemonCommand()``)."""

import json
import os
import struct
import sys

token = sys.argv[sys.argv.index("--token") + 1]
stdout = sys.stdout.buffer
stdout.write(f"PI-ENV {token}\n".encode())
stdout.flush()


def send(type: int, id: int, body: dict) -> None:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    stdout.write(struct.pack(">IBII", 9 + len(data), type, id, len(data)) + data)
    stdout.flush()


buffered = b""
while True:
    chunk = os.read(sys.stdin.fileno(), 64 * 1024)
    if not chunk:
        sys.exit(0)
    buffered += chunk
    while len(buffered) >= 4 and len(buffered) >= 4 + int.from_bytes(buffered[0:4], "big"):
        length = int.from_bytes(buffered[0:4], "big")
        type = buffered[4]
        id = int.from_bytes(buffered[5:9], "big")
        jsonLength = int.from_bytes(buffered[9:13], "big")
        request = json.loads(buffered[13 : 13 + jsonLength].decode("utf-8"))
        buffered = buffered[4 + length :]
        if type != 1:
            continue
        if request.get("op") == "hello":
            send(
                2,
                id,
                {
                    "protocol": 1,
                    "version": "1.1.0",
                    "os": "linux",
                    "arch": "x86_64",
                    "home": "/",
                    "separator": "/",
                    "tmpdir": "/tmp",
                    "cwd": "/",
                    "driveCwds": {},
                    "pid": os.getpid(),
                },
            )
        else:
            send(3, id, {"code": "EINVAL", "message": f"unknown operation {request.get('op')}"})
