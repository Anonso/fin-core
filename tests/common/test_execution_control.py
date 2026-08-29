from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from fin_analyse.common.execution_control import (
    BoundedExecutor,
    ExecutionFence,
    ExecutorCapacityError,
)


def test_bounded_executor_rejects_work_beyond_fixed_outstanding_capacity() -> None:
    release = Event()
    executor = BoundedExecutor(
        max_workers=1,
        max_outstanding=1,
        thread_name_prefix="test-execution-capacity",
    )
    try:
        first = executor.submit(release.wait, 1)
        with pytest.raises(ExecutorCapacityError):
            executor.submit(lambda: None)
        release.set()
        assert first.result(timeout=1) is True
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_fence_cancellation_linearizes_after_active_publication() -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    fence = ExecutionFence(now + timedelta(seconds=30))
    publication_started = Event()
    release_publication = Event()
    cancellation_finished = Event()

    def publish() -> None:
        with fence.publication(at=now) as allowed:
            assert allowed is True
            publication_started.set()
            assert release_publication.wait(timeout=1)

    publisher = Thread(target=publish, name="test-publication-fence")
    publisher.start()
    assert publication_started.wait(timeout=1)

    canceller = Thread(
        target=lambda: (fence.cancel(), cancellation_finished.set()),
        name="test-cancellation-fence",
    )
    canceller.start()
    assert not cancellation_finished.wait(timeout=0.02)

    release_publication.set()
    publisher.join(timeout=1)
    canceller.join(timeout=1)
    assert cancellation_finished.is_set()
    with fence.publication(at=now) as allowed:
        assert allowed is False
