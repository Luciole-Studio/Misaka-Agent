"""Pending skill writes for a person at the keyboard.

The model can stage a change. Approving or rejecting it happens here, from the
GUI or the terminal, never from a marked agent session.
"""
from pathlib import Path

_DIFF_LIMIT = 20000


def pending_items(*, with_diff=False):
    """One row per staged write: the source directories and any startup copies."""
    from . import write as skill_write
    from .sandbox import copies_of

    rows = []
    for record in skill_write.list_pending():
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        bases = payload.get("bases") if isinstance(payload.get("bases"), dict) else {}
        sources = [str(Path(path)) for path in bases]
        copies = []
        for path in sources:
            copies.extend(copies_of(path))
        row = {
            "id": record.get("_pending_file_id") or record.get("id") or "",
            "summary": record.get("summary") or "",
            "origin": record.get("origin") or "",
            "error": record.get("_integrity_error") or "",
            "sources": sources,
            "copies": copies,
        }
        if with_diff and not row["error"] and row["id"]:
            diff = skill_write.pending_diff(record)
            if isinstance(diff, str) and len(diff) > _DIFF_LIMIT:
                diff = diff[:_DIFF_LIMIT] + "\n…（差异过长，已截断）"
            row["diff"] = diff or ""
        rows.append(row)
    return rows


def decide(pending_id, decision):
    """Approve or reject one staged write. A marked agent session cannot call this."""
    from . import manage, write as skill_write

    if skill_write.agent_session():
        raise ValueError("Marked agent sessions do not make skill review decisions.")
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    if not isinstance(pending_id, str) or not pending_id.strip():
        raise ValueError("缺少待审编号")
    record = skill_write.get_pending(pending_id)
    if record is None:
        raise ValueError("没有这条待审记录")
    file_id = record.get("_pending_file_id") or record.get("id")
    if record.get("_integrity_error"):
        raise ValueError(record["_integrity_error"])
    if decision == "reject":
        skill_write.discard_pending(file_id)
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        names = payload.get("bound_names") or []
        skill_write.record("reject", ", ".join(names), evidence={"pending_id": record.get("id")})
        return {"rejected": True, "id": file_id}
    result = manage.apply_pending(record)
    if not result.get("success"):
        raise ValueError(result.get("error") or "批准失败")
    skill_write.discard_pending(file_id)
    return {
        "approved": True,
        "id": file_id,
        "paths": result.get("written_paths") or [],
        "copies": result.get("updated_copies") or [],
        "copy_errors": result.get("copy_errors") or [],
    }
