"""The single writer of stdout. Control frames (results, errors, pings, watch events) always go before bulk command
output, so a slow link delays output but never a cancel's result or a ping. Bulk output is bounded: readers of commands
without an output window wait while too much of it is unsent, which slows the command down like a full pipe would."""

from __future__ import annotations

import collections
import threading
from typing import BinaryIO

from .frame import Frame, write_frame

# Unsent bulk bytes above which unwindowed command output waits.
BULK_LIMIT = 4 * 1024 * 1024


class Counter:
    """``Arc<AtomicUsize>``: frames of one sender that are not written yet."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def add(self, amount: int) -> None:
        with self._lock:
            self._value += amount

    def load(self) -> int:
        with self._lock:
            return self._value


class Output:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.control_frames: collections.deque[Frame] = collections.deque()
        self.bulk_frames: collections.deque[tuple[Frame, Counter | None]] = collections.deque()
        self.bulk_bytes = 0
        self.closed = False
        # Signalled when a frame is queued or the writer stops.
        self.queued = threading.Condition(self._lock)
        # Signalled when bulk bytes were written.
        self.drained = threading.Condition(self._lock)

    def control(self, frame: Frame) -> None:
        with self._lock:
            self.control_frames.append(frame)
            self.queued.notify()

    def bulk(self, frame: Frame, unsent: Counter | None) -> None:
        """Queue bulk output; ``unsent`` counts this sender's frames until they are written."""
        with self._lock:
            self.bulk_bytes += len(frame.payload)
            if unsent is not None:
                unsent.add(1)
            self.bulk_frames.append((frame, unsent))
            self.queued.notify()

    def wait_for_room(self, stop: threading.Event) -> None:
        """Wait while more than the bulk limit is unsent, unless ``stop`` is set."""
        with self._lock:
            while self.bulk_bytes > BULK_LIMIT and not self.closed and not stop.is_set():
                self.drained.wait(0.1)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self.queued.notify_all()
            self.drained.notify_all()

    def run(self, writer: BinaryIO) -> None:
        """Write frames to ``writer`` until closed or a write fails."""
        while True:
            with self._lock:
                while True:
                    if self.control_frames:
                        frame, unsent = self.control_frames.popleft(), None
                        break
                    if self.bulk_frames:
                        frame, unsent = self.bulk_frames.popleft()
                        self.bulk_bytes -= len(frame.payload)
                        break
                    if self.closed:
                        return
                    self.queued.wait()
            try:
                write_frame(writer, frame)
                failed = False
            except (OSError, ValueError):
                failed = True
            if unsent is not None:
                unsent.add(-1)
            with self._lock:
                self.drained.notify_all()
            if failed:
                self.close()
                return
