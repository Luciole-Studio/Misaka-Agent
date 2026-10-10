"""A stub GUI chat runner for ChatManager contract tests: no model, no home writes.

Speaks the same line-JSON protocol as misaka.ui.gui.chat_runner, echoing ops so
the manager's pump, event ring, request matching and shutdown can be tested
without assembling a real session.
"""
import json
import sys


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    emit({"type": "ready", "sessionId": "stub-session", "sessionFile": None,
          "cwd": spec.get("workspace"), "name": None, "model": None,
          "thinkingLevel": "off", "availableThinkingLevels": ["off"],
          "modelFallbackMessage": None, "streaming": False})
    emit({"type": "history", "messages": []})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        op = request.get("op")
        if op == "shutdown":
            emit({"type": "result", "id": request.get("id"), "ok": True, "data": {"bye": True}})
            break
        if op == "boom":
            emit({"type": "result", "id": request.get("id"), "ok": False, "error": "炸了"})
            continue
        if op == "note":
            emit({"type": "event", "event": {"type": "note", "text": (request.get("params") or {}).get("text", "")}})
        emit({"type": "result", "id": request.get("id"), "ok": True, "data": {"op": op}})


if __name__ == "__main__":
    main()
