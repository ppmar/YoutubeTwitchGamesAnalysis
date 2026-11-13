"""Task queue and crash recovery (SPEC section 8, milestone 2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ytcollector.models import Task
from ytcollector.tasks import TaskQueue


@pytest.fixture
def queue(session: object) -> TaskQueue:
    return TaskQueue(session, study="battle_royale")  # type: ignore[arg-type]


class TestEnqueue:
    def test_creates_a_pending_task(self, queue: TaskQueue) -> None:
        task = queue.enqueue("search", "fortnite:2018-07", payload={"game_id": "fortnite"})
        assert task.status == "pending"
        assert task.payload == {"game_id": "fortnite"}

    def test_is_idempotent_on_the_task_key(self, queue: TaskQueue) -> None:
        queue.enqueue("search", "fortnite:2018-07")
        queue.enqueue("search", "fortnite:2018-07")
        assert queue.count("search") == 1

    def test_does_not_resurrect_a_finished_task(self, queue: TaskQueue) -> None:
        task = queue.enqueue("search", "fortnite:2018-07")
        queue.complete(task)
        queue.enqueue("search", "fortnite:2018-07")
        assert queue.count("search", status="done") == 1
        assert queue.count("search", status="pending") == 0

    def test_separates_kinds_and_studies(self, session: object) -> None:
        a = TaskQueue(session, study="battle_royale")  # type: ignore[arg-type]
        b = TaskQueue(session, study="souls_like")  # type: ignore[arg-type]
        a.enqueue("search", "x")
        a.enqueue("videos", "x")
        b.enqueue("search", "x")
        assert a.count("search") == 1
        assert a.count("videos") == 1
        assert b.count("search") == 1


class TestClaim:
    def test_returns_tasks_in_priority_order(self, queue: TaskQueue) -> None:
        queue.enqueue("search", "low", priority=10)
        queue.enqueue("search", "high", priority=1)
        assert queue.claim("search").task_key == "high"

    def test_marks_the_task_running(self, queue: TaskQueue) -> None:
        queue.enqueue("search", "x")
        task = queue.claim("search")
        assert task.status == "running"
        assert task.started_at is not None

    def test_does_not_hand_out_the_same_task_twice(self, queue: TaskQueue) -> None:
        queue.enqueue("search", "x")
        queue.claim("search")
        assert queue.claim("search") is None

    def test_returns_none_when_the_queue_is_empty(self, queue: TaskQueue) -> None:
        assert queue.claim("search") is None


class TestOutcomes:
    def test_complete_marks_done(self, queue: TaskQueue) -> None:
        task = queue.claim_or_enqueue("search", "x")
        queue.complete(task)
        assert task.status == "done"
        assert task.finished_at is not None

    def test_fail_records_the_error_and_requeues(self, queue: TaskQueue) -> None:
        task = queue.claim_or_enqueue("search", "x")
        queue.fail(task, "connection reset")
        assert task.status == "pending"
        assert task.attempts == 1
        assert task.last_error == "connection reset"

    def test_too_many_attempts_gives_up(self, queue: TaskQueue) -> None:
        task = queue.claim_or_enqueue("search", "x")
        for _ in range(queue.max_attempts):
            queue.fail(task, "boom")
        assert task.status == "failed"
        assert queue.claim("search") is None

    def test_a_rerun_skips_finished_work(self, queue: TaskQueue) -> None:
        for key in ("a", "b", "c"):
            queue.enqueue("search", key)
        queue.complete(queue.claim("search"))
        remaining = [queue.claim("search"), queue.claim("search")]
        assert queue.claim("search") is None
        assert len([t for t in remaining if t]) == 2
        assert queue.count("search", status="done") == 1


class TestOrphanRecovery:
    def test_running_tasks_return_to_pending(self, queue: TaskQueue) -> None:
        # A container killed mid-run leaves its task `running` forever.
        queue.enqueue("search", "x")
        queue.claim("search")
        assert queue.count("search", status="running") == 1

        recovered = queue.recover_orphans()

        assert recovered == 1
        assert queue.count("search", status="pending") == 1
        assert queue.claim("search") is not None

    def test_finished_tasks_are_left_alone(self, queue: TaskQueue) -> None:
        done = queue.claim_or_enqueue("search", "done")
        queue.complete(done)
        queue.enqueue("search", "pending")
        assert queue.recover_orphans() == 0
        assert queue.count("search", status="done") == 1

    def test_recovery_is_scoped_to_the_study(self, session: object) -> None:
        a = TaskQueue(session, study="battle_royale")  # type: ignore[arg-type]
        b = TaskQueue(session, study="souls_like")  # type: ignore[arg-type]
        a.claim_or_enqueue("search", "x")
        b.claim_or_enqueue("search", "y")
        assert a.recover_orphans() == 1
        assert b.count("search", status="running") == 1


class TestCursor:
    def test_cursor_survives_between_runs(self, queue: TaskQueue) -> None:
        # A big video's comments must resume, not restart, the next day.
        task = queue.claim_or_enqueue("comments", "video:abc")
        queue.pause(task, cursor="PAGE_TOKEN_2")
        assert task.status == "pending"
        assert task.cursor == "PAGE_TOKEN_2"

        resumed = queue.claim("comments")
        assert resumed.cursor == "PAGE_TOKEN_2"

    def test_completing_clears_the_cursor(self, queue: TaskQueue) -> None:
        task = queue.claim_or_enqueue("comments", "video:abc")
        queue.pause(task, cursor="PAGE_TOKEN_2")
        queue.complete(queue.claim("comments"))
        row = queue.session.scalar(select(Task).where(Task.task_key == "video:abc"))
        assert row.cursor is None
