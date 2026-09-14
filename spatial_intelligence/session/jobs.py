"""Progress jobs: the session's :class:`ProgressSink`.

Every tool that reports progress emits into this registry, which keeps the
latest snapshot per job and forwards it to the browser as a ``job`` event.
Its lock is deliberately separate from the session lock: a prompt run no longer
blocks REST mutations, but a download thread may still report while another
thread is editing the notebook.
"""

from __future__ import annotations

import threading

from ..contracts.progress import ProgressEvent, ProgressSink, Reporter
from .bus import EventBus


class JobRegistry(ProgressSink):
    """Latest snapshot of every progress job, plus the events that carry them."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()

    def emit(self, event: ProgressEvent) -> None:
        """Store a job snapshot and publish it (never blocks on the session)."""
        payload = event.as_dict()
        with self._lock:
            self._jobs[event.job_id] = payload
        self._bus.publish("job", payload)

    def reporter(self, *, parent_id: str | None = None) -> Reporter:
        """Return a reporter whose jobs belong to ``parent_id``."""
        return Reporter(self, parent_id=parent_id)

    def snapshot(self) -> list[dict]:
        """Return every job snapshot (completion order)."""
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def clear(self) -> None:
        """Forget every job (workspace switch)."""
        with self._lock:
            self._jobs.clear()

    def clear_parent(self, parent_id: str | None) -> None:
        """Forget the jobs owned by one cell (its re-run or deletion)."""
        if parent_id is None:
            return
        with self._lock:
            self._jobs = {
                job_id: job
                for job_id, job in self._jobs.items()
                if job.get("parent_id") != parent_id
            }
