"""Owner-side GUI follow-ups; promotion uses the session's step-boundary steering."""
from __future__ import annotations

import asyncio
import uuid


class GuiInputQueue:
    def __init__(self, session, deliver, changed=lambda: None, failed=lambda error: None, settled=lambda: None):
        self.session = session
        self.deliver = deliver
        self.changed, self.failed, self.settled = changed, failed, settled
        self.pending = []
        self.task = None
        self.ready = asyncio.Event()
        self.ready.set()

    @property
    def paused(self):
        return not self.ready.is_set()

    def pause(self):
        self.ready.clear()
        self.changed()

    def resume(self, message_id=None):
        if message_id is not None:
            item = next((p for p in self.pending if p["message_id"] == message_id), None)
            if item is not None:
                self.pending.remove(item)
                self.pending.insert(0, item)
        self.ready.set()
        self.changed()
        if self.pending:
            self._schedule()

    def snapshot(self):
        return [{"id": p["message_id"], "text": p.get("display_text") or p["text"],
                 "images": len(p.get("images") or [])} for p in self.pending]

    def submit(self, params):
        if len(self.pending) >= 100:
            raise ValueError("排队消息已达 100 条，请等待处理后再发送")
        item = {**params, "message_id": params.get("message_id") or uuid.uuid4().hex}
        if not isinstance(item["message_id"], str) or len(item["message_id"]) > 128:
            raise ValueError("消息标识无效")
        if any(p["message_id"] == item["message_id"] for p in self.pending):
            return {"accepted": True, "message_id": item["message_id"]}
        self.pending.append(item)
        self.changed()
        self._schedule()
        return {"accepted": True, "message_id": item["message_id"]}

    def _schedule(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())

    async def _drain(self):
        while self.pending:
            await self.ready.wait()
            await self.session.waitForIdle()
            await self.ready.wait()
            if not self.pending:
                break
            item = self.pending.pop(0)
            self.changed()
            try:
                await self.deliver(item)
                await asyncio.sleep(0)
                await self.session.waitForIdle()
            except Exception as error:  # noqa: BLE001 - retain visible delivery errors
                self.failed(error)
        self.settled()

    async def send_now(self, message_id):
        item = next((p for p in self.pending if p["message_id"] == message_id), None)
        if item is None:
            raise ValueError("这条消息已开始处理，请等待回复")
        self.pending.remove(item)
        if self.session.isStreaming:
            try:
                await self.deliver({**item, "streamingBehavior": "steer"})
            except Exception:
                self.pending.insert(0, item)
                self.changed()
                raise
        else:
            self.pending.insert(0, item)
            self._schedule()
        self.resume()
        self.changed()
        return {"accepted": True, "message_id": message_id, "steering": self.session.isStreaming}

    def withdraw(self, message_id):
        item = next((p for p in self.pending if p["message_id"] == message_id), None)
        if item is None:
            raise ValueError("这条消息已开始处理，无法撤回")
        self.pending.remove(item)
        self.changed()
        return {"withdrawn": True, "message_id": message_id,
                "text": item.get("display_text") or item["text"],
                "images": item.get("images") or [], "files": item.get("files") or [],
                "pendingPrompts": self.snapshot()}

    def clear(self):
        self.pending.clear()
        self.changed()
        if self.task is not None and not self.task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if self.task is not current:
                self.task.cancel()
