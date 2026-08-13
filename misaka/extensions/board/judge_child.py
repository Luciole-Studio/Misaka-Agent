"""Isolated red-team verifier process for addressable Sister cards."""

from __future__ import annotations

import contextlib
import json
import sys
import traceback

from misaka.extensions.board import db, dispatch


REQUIRED_CFG = frozenset({"db", "provider", "default_model", "roles_root"})


def main() -> int:
    con = None
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or request.get("version") != 1:
            raise ValueError("bad judge protocol version")
        task_id = request.get("task_id")
        token = request.get("verify_token")
        generation = request.get("generation")
        cfg = request.get("cfg")
        if not isinstance(task_id, str) or not isinstance(token, str):
            raise ValueError("bad judge task/token")
        if not isinstance(cfg, dict) or not REQUIRED_CFG <= cfg.keys():
            raise ValueError("incomplete judge config")

        con = db.connect(str(cfg["db"]))
        row = db.get(con, task_id)
        if row is None:
            raise ValueError(f"unknown judge task: {task_id}")
        if generation is None:
            generation = int(row["generation"])
        if type(generation) is not int:
            raise ValueError("bad judge generation")
        # Keep stdout as a one-frame control channel even if a provider emits
        # diagnostics while the verifier is running.
        with contextlib.redirect_stdout(sys.stderr):
            dispatch.judge_task(con, row, cfg, token, generation=generation)
        state = db.get(con, task_id)
        print(
            json.dumps(
                {"version": 1, "ok": True, "task_id": task_id, "status": state["status"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0
    except Exception as error:  # noqa: BLE001 - process boundary reports all failures
        traceback.print_exc(file=sys.stderr)
        print(
            json.dumps(
                {"version": 1, "ok": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 2
    finally:
        if con is not None:
            con.close()


if __name__ == "__main__":
    raise SystemExit(main())
