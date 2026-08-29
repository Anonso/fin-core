"""Small process-local controls for bounded advisory work.

These controls do not attempt to kill Python threads.  They make the two
properties FIN actually needs explicit instead:

* a fixed upper bound on running and queued work; and
* a cooperative fence that prevents a late result from being published after
  its caller has timed out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from threading import BoundedSemaphore, Event, Lock
from typing import Any, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")


class ExecutorCapacityError(RuntimeError):
    """The bounded executor has no safe running or queue capacity left."""


class BoundedExecutor:
    """A reusable thread pool with a hard cap on outstanding futures."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_outstanding: int,
        thread_name_prefix: str,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_outstanding < max_workers:
            raise ValueError("max_outstanding must be at least max_workers")
        self._max_workers = max_workers
        self._max_outstanding = max_outstanding
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._capacity = BoundedSemaphore(max_outstanding)

    def submit(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        if not self._capacity.acquire(blocking=False):
            raise ExecutorCapacityError("bounded executor capacity exhausted")
        try:
            future = self._executor.submit(function, *args, **kwargs)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(self._release_capacity)
        return future

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def max_outstanding(self) -> int:
        return self._max_outstanding

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        """Explicit lifecycle seam, primarily for isolated compositions/tests."""

        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _release_capacity(self, _future: Future[Any]) -> None:
        self._capacity.release()


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    """Absolute deadline plus cancellation state for late-publication fencing."""

    deadline_at: datetime
    _cancelled: Event = field(default_factory=Event, init=False, repr=False, compare=False)
    _publication_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("execution fence deadline_at must be timezone-aware")

    def cancel(self) -> None:
        # Linearize cancellation against the small FIN-owned publication
        # section.  Once this returns, no holder can begin or remain inside a
        # protected state/artifact write.
        with self._publication_lock:
            self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def is_open(self, *, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("execution fence observation must be timezone-aware")
        return not self.cancelled and at < self.deadline_at

    def remaining_seconds(self, *, at: datetime) -> float:
        if not self.is_open(at=at):
            return 0.0
        return max(0.0, (self.deadline_at - at).total_seconds())

    @contextmanager
    def publication(self, *, at: datetime) -> Iterator[bool]:
        """Serialize one state/artifact publication against cancellation."""

        with self._publication_lock:
            yield self.is_open(at=at)
