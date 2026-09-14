"""Job progress: the single channel a slow tool uses to report to the browser.

A tool that can outlive a moment (a download, a raster warp, a ``run_python``
snippet) opens a :class:`Job` from its runtime reporter. Every job emits
immutable :class:`ProgressEvent` snapshots to a sink supplied by the session
layer, which forwards them over SSE. Tools that return promptly ignore the
reporter entirely, and the same tool code runs against :data:`NULL_REPORTER` in
tests and headless use.

Events are deliberately generic: the browser renders one progress component per
job instead of one per *kind* of job.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Protocol, runtime_checkable

from .ids import new_id


class JobState(StrEnum):
    """Lifecycle of a progress-tracked job."""

    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


#: Terminal states; a job never leaves one of these.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.DONE, JobState.ERROR, JobState.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One immutable snapshot of a job, ready to serialize for the browser."""

    job_id: str
    kind: str
    status: JobState
    label: str
    unit: str = "units"
    completed: float | None = None
    total: float | None = None
    detail: str | None = None
    artifact: str | None = None
    error: str | None = None
    parent_id: str | None = None

    def as_dict(self) -> dict:
        """Return the JSON-safe payload for the SSE consumer."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status.value,
            "label": self.label,
            "unit": self.unit,
            "completed": self.completed,
            "total": self.total,
            "detail": self.detail,
            "artifact": self.artifact,
            "error": self.error,
            "parent_id": self.parent_id,
        }


@runtime_checkable
class ProgressSink(Protocol):
    """Where progress events go (the session layer implements this)."""

    def emit(self, event: ProgressEvent) -> None: ...


_UNSET = object()


class Job:
    """One progress-tracked unit of work.

    Thread-safe: ``download_files`` and catalog searches report from worker
    threads. Once terminal, further reports are ignored so a straggling sibling
    thread cannot resurrect a finished job.
    """

    __slots__ = (
        "_sink",
        "_kind",
        "_label",
        "_unit",
        "_parent_id",
        "_lock",
        "_state",
        "_completed",
        "_total",
        "_detail",
        "_artifact",
        "_error",
        "id",
    )

    def __init__(
        self,
        sink: ProgressSink | None,
        *,
        job_id: str,
        kind: str,
        label: str,
        unit: str = "units",
        total: float | None = None,
        parent_id: str | None = None,
    ) -> None:
        self.id = job_id
        self._sink = sink
        self._kind = kind
        self._label = label
        self._unit = unit
        self._parent_id = parent_id
        self._lock = RLock()
        self._state = JobState.RUNNING
        self._completed: float = 0.0
        self._total = total
        self._detail: str | None = None
        self._artifact: str | None = None
        self._error: str | None = None
        self._emit()

    # -- reporting -------------------------------------------------------

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    @property
    def parent_id(self) -> str | None:
        return self._parent_id

    def progress(
        self,
        completed: float | None = None,
        *,
        total: float | None = None,
        detail: str | None = None,
    ) -> None:
        """Report absolute progress (``completed``/``total`` in ``unit``)."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            if completed is not None:
                self._completed = float(completed)
            if total is not None:
                self._total = float(total)
            if detail is not None:
                self._detail = detail
            self._emit()

    def advance(self, delta: float = 1.0, *, detail: str | None = None) -> None:
        """Report relative progress."""
        self.progress(self._completed + float(delta), detail=detail)

    def done(self, *, artifact: str | None = None, detail: str | None = None) -> None:
        """Mark the job complete, optionally pointing at its output.

        A completed job is fully progressed: when a total is known, the reported
        completion moves to it, so the UI never shows a finished job at 0%.
        """
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            if artifact is not None:
                self._artifact = artifact
            if detail is not None:
                self._detail = detail
            if self._total is not None:
                self._completed = self._total
            self._state = JobState.DONE
            self._emit()

    def fail(self, error: object) -> None:
        """Mark the job failed with a human-readable reason."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._error = str(error)
            self._state = JobState.ERROR
            self._emit()

    def cancel(self, *, detail: str | None = None) -> None:
        """Mark the job cancelled."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            if detail is not None:
                self._detail = detail
            self._state = JobState.CANCELLED
            self._emit()

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # A clean exit completes the job; an escaping exception fails it. Both
        # are no-ops when the body already reported a terminal state. The
        # exception is never swallowed.
        if exc is None:
            self.done()
        else:
            self.fail(exc)
        return False

    # -- internals -------------------------------------------------------

    def _emit(self) -> None:
        sink = self._sink
        if sink is None:
            return
        event = ProgressEvent(
            job_id=self.id,
            kind=self._kind,
            status=self._state,
            label=self._label,
            unit=self._unit,
            completed=self._completed,
            total=self._total,
            detail=self._detail,
            artifact=self._artifact,
            error=self._error,
            parent_id=self._parent_id,
        )
        try:
            sink.emit(event)
        except Exception:
            # Progress is an observability path: a disconnected or broken
            # subscriber must never fail the work being reported.
            pass


class Reporter:
    """Factory for jobs bound to one sink and one owning run.

    A session hands each run (or each out-of-band call) its own reporter, so
    every job automatically carries the run it belongs to as ``parent_id``.
    """

    __slots__ = ("_sink", "_parent_id")

    def __init__(
        self, sink: ProgressSink | None = None, *, parent_id: str | None = None
    ) -> None:
        self._sink = sink
        self._parent_id = parent_id

    @property
    def sink(self) -> ProgressSink | None:
        return self._sink

    @property
    def parent_id(self) -> str | None:
        return self._parent_id

    def job(
        self,
        kind: str,
        label: str,
        *,
        unit: str = "units",
        total: float | None = None,
        parent_id: object = _UNSET,
    ) -> Job:
        """Open a job of ``kind`` (``download``, ``raster``, ``python``, ...)."""
        return Job(
            self._sink,
            job_id=new_id(),
            kind=kind,
            label=label,
            unit=unit,
            total=total,
            parent_id=self._parent_id if parent_id is _UNSET else parent_id,  # type: ignore[arg-type]
        )

    def rebind(
        self, *, sink: ProgressSink | None = None, parent_id: str | None = None
    ) -> "Reporter":
        """Return a reporter for the same session with a different owner."""
        return Reporter(
            self._sink if sink is None else sink,
            parent_id=self._parent_id if parent_id is None else parent_id,
        )


#: No-op reporter for tests, headless runs, and tools called out of band.
NULL_REPORTER = Reporter()
