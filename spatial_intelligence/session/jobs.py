"""Progress jobs: the session's :class:`ProgressSink`.

Every tool that reports progress emits into this registry, which keeps the
latest snapshot per job and forwards it to the browser as a ``job`` event; when
the registry forgets jobs (a re-run, a deleted cell, a workspace switch) the
remaining list goes out as a ``jobs`` event, because the browser keeps its own
copy. Its lock is deliberately separate from the session lock: a prompt run no
longer blocks REST mutations, but a download thread may still report while
another thread is editing the notebook.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

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

    def reporter(
        self,
        *,
        parent_id: str | None = None,
        anchor: Callable[[], int | None] | None = None,
    ) -> Reporter:
        """Return a reporter whose jobs belong to ``parent_id``.

        ``anchor`` is asked for the owning cell's published step count each time
        a job opens, so the browser can draw that job's card at the step it
        belongs to.
        """
        return Reporter(self, parent_id=parent_id, anchor=anchor)

    def snapshot(self) -> list[dict]:
        """Return every job snapshot (completion order)."""
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def clear(self) -> None:
        """Forget every job (workspace switch)."""
        self._prune(lambda job: True)

    def clear_parent(self, parent_id: str | None) -> None:
        """Forget the jobs owned by one cell (its re-run or deletion)."""
        if parent_id is None:
            return
        self._prune(lambda job: job.get("parent_id") == parent_id)

    def _prune(self, drop: Callable[[dict], bool]) -> None:
        """Drop the jobs matching ``drop`` and publish what is left.

        The browser holds its own copy of the job list, so a job the session
        forgets — a re-run clears the downloads of the cell it restarts, a
        deleted cell takes its cards with it — has to be announced. Otherwise
        the card of a job that no longer exists stays on screen until the next
        full snapshot.
        """
        with self._lock:
            self._jobs = {
                job_id: job for job_id, job in self._jobs.items() if not drop(job)
            }
            jobs = [dict(job) for job in self._jobs.values()]
        self._bus.publish("jobs", {"jobs": jobs})
