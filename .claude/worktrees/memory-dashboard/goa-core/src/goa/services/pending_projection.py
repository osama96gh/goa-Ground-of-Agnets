"""Per-process derived view of §7 pending-questions state.

`Task.pending_questions` is not persisted; this module is the projection
rebuildable from the event log per spec §7 push/pop rules:

- `question` event → push `(event.id, target)` for each target in payload.to
- `answer` event → pop `(qid, answerer)` for each qid in payload.answering
  (first-answer-wins per target — only the answerer's pair drops)
- `cancel_question` event → drop all `(qid, *)` for each qid in payload.retracts
- `cancel_all_questions` event → clear the entire list
- All other event types → no-op

`PendingProjection` is the single in-process source of truth for pending
state. Its cache is opportunistic: cold cache misses rebuild from the log
under the per-task `TaskLog.lock`, preserving consistency with concurrent
`apply()` calls from event-emit paths.
"""

from __future__ import annotations

from uuid import UUID

from goa.domain.models import Event, PendingPair
from goa.repos.protocols import TaskLog


class PendingProjection:
    """Per-process pending-questions projection. See module docstring for §7 rules.

    Locking discipline:
    - `apply(event)` is lock-free internally; callers MUST hold
      `self._log.lock(event.task_id)` (the same lock that serializes
      `append_event` for the task).
    - `get(task_id)` returns the cached state in the common case. On a
      cold cache, it acquires `self._log.lock(task_id)` and rebuilds from
      `list_events_for_task`. Double-checked locking handles concurrent
      first-time reads.
    """

    def __init__(self, log: TaskLog) -> None:
        self._log = log
        self._state: dict[UUID, list[PendingPair]] = {}

    async def get(self, task_id: UUID) -> list[PendingPair]:
        """Return the current pending pairs for `task_id`.

        Cold cache: rebuilds under the per-task lock to prevent a concurrent
        `apply()` from being overwritten by a stale rebuild. Returns a copy
        so callers cannot mutate the cache.
        """
        cached = self._state.get(task_id)
        if cached is not None:
            return list(cached)
        async with self._log.lock(task_id):
            cached = self._state.get(task_id)
            if cached is not None:  # someone rebuilt while we waited
                return list(cached)
            state: list[PendingPair] = []
            for ev in await self._log.list_events_for_task(task_id):
                state = self._apply_event(state, ev)
            self._state[task_id] = state
            return list(state)

    async def apply(self, event: Event) -> None:
        """Advance the projection by one event.

        Caller MUST already hold `self._log.lock(event.task_id)`. This is
        the same lock that serializes `append_event` for the task, so
        `apply` is naturally serialized with respect to itself and with
        `get`'s cold-rebuild path.

        `apply` is called on every event (not just pending-mutating ones)
        so the cache is warm for any task that has ever seen any event in
        this process — this prevents `_fanout` (which runs inside the lock
        and calls `get`) from triggering the cold-rebuild path and
        re-acquiring its own lock.
        """
        current = self._state.get(event.task_id, [])
        self._state[event.task_id] = self._apply_event(current, event)

    async def invalidate(self, task_id: UUID) -> None:
        """Drop the cached state for `task_id`. The next `get` rebuilds."""
        self._state.pop(task_id, None)

    @staticmethod
    def _apply_event(state: list[PendingPair], event: Event) -> list[PendingPair]:
        """Pure §7 transition. Returns a new list; does not mutate input.

        Unknown event types are a no-op — `info`, `participant_joined`,
        `child_task_created`, `parent_closed` all leave pending unchanged.
        """
        match event.event_type:
            case "question":
                # Push (event.id, target) for each target.
                return state + [(event.id, t) for t in event.payload.to]
            case "answer":
                # First-answer-wins per target: drop only the answerer's pair
                # for each qid being answered. Other targets remain pending.
                answering = set(event.payload.answering)
                return [
                    (qid, t)
                    for (qid, t) in state
                    if not (qid in answering and t == event.from_)
                ]
            case "cancel_question":
                # Retract entire questions: drop all (qid, *) for each qid.
                retracts = set(event.payload.retracts)
                return [(qid, t) for (qid, t) in state if qid not in retracts]
            case "cancel_all_questions":
                return []
            case _:
                # info, participant_joined, child_task_created, parent_closed — no-op
                return state
