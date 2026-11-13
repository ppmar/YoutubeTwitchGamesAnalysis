"""Task queue with crash recovery (SPEC section 8).

The collector is killed every day by its own quota, and Railway can stop the
container at any point. Work is therefore persisted as tasks: a rerun picks up
what is left instead of redoing what is done, and a task left ``running`` by a
brutal stop goes back to ``pending`` at the next start.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector.models import Task, utcnow

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


class TaskQueue:
    """Queue scoped to one study."""

    def __init__(self, session: Session, study: str, max_attempts: int = 5) -> None:
        self.session = session
        self.study = study
        self.max_attempts = max_attempts

    def _get(self, kind: str, task_key: str) -> Task | None:
        return self.session.scalar(
            select(Task).where(
                Task.study == self.study, Task.kind == kind, Task.task_key == task_key
            )
        )

    def enqueue(
        self,
        kind: str,
        task_key: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> Task:
        """Add a task, or return the existing one.

        Planning runs daily and re-proposes the same work, so enqueueing has to be
        idempotent and must never reopen a task that already finished.
        """
        existing = self._get(kind, task_key)
        if existing is not None:
            return existing

        task = Task(
            study=self.study,
            kind=kind,
            task_key=task_key,
            payload=payload or {},
            priority=priority,
            status=PENDING,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def claim(self, kind: str) -> Task | None:
        """Take the next pending task of this kind and mark it running."""
        task = self.session.scalars(
            select(Task)
            .where(Task.study == self.study, Task.kind == kind, Task.status == PENDING)
            .order_by(Task.priority, Task.id)
            .limit(1)
        ).first()
        if task is None:
            return None
        task.status = RUNNING
        task.started_at = utcnow()
        self.session.flush()
        return task

    def claim_or_enqueue(self, kind: str, task_key: str, **kwargs: Any) -> Task:
        """Convenience for callers that know exactly which task they want."""
        self.enqueue(kind, task_key, **kwargs)
        task = self._get(kind, task_key)
        assert task is not None
        task.status = RUNNING
        task.started_at = utcnow()
        self.session.flush()
        return task

    def complete(self, task: Task) -> None:
        task.status = DONE
        task.finished_at = utcnow()
        task.last_error = None
        task.cursor = None
        self.session.flush()

    def fail(self, task: Task, error: str) -> None:
        """Record a failure and requeue, until the attempt budget runs out."""
        task.attempts += 1
        task.last_error = error[:2000]
        task.status = FAILED if task.attempts >= self.max_attempts else PENDING
        if task.status == FAILED:
            task.finished_at = utcnow()
        self.session.flush()

    def pause(self, task: Task, cursor: str | None) -> None:
        """Stop mid-task and remember where to resume.

        Used when a long video's comments outlive the day's quota: the page token
        is kept so tomorrow continues instead of starting the video again.
        """
        task.cursor = cursor
        task.status = PENDING
        self.session.flush()

    def recover_orphans(self, kind: str | None = None) -> int:
        """Return tasks stuck in ``running`` to ``pending``.

        Called at startup: a container that died mid-task left its row claimed, and
        nothing would ever pick it up again.
        """
        query = select(Task).where(Task.study == self.study, Task.status == RUNNING)
        if kind is not None:
            query = query.where(Task.kind == kind)
        orphans = list(self.session.scalars(query))
        for task in orphans:
            task.status = PENDING
            task.started_at = None
        self.session.flush()
        return len(orphans)

    def count(self, kind: str | None = None, status: str | None = None) -> int:
        query = select(func.count()).select_from(Task).where(Task.study == self.study)
        if kind is not None:
            query = query.where(Task.kind == kind)
        if status is not None:
            query = query.where(Task.status == status)
        return int(self.session.scalar(query) or 0)

    def progress(self, kind: str) -> dict[str, int]:
        """Counts per status, for `ytc status` and the daily log line."""
        rows = self.session.execute(
            select(Task.status, func.count())
            .where(Task.study == self.study, Task.kind == kind)
            .group_by(Task.status)
        ).all()
        counts = {status: int(count) for status, count in rows}
        return {s: counts.get(s, 0) for s in (PENDING, RUNNING, DONE, FAILED)}
