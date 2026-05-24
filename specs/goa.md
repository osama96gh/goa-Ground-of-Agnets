# Goa — Ground of Agents

**Status:** v2 — design
**Last updated:** 2026-05-19
**Owner:** Osama

This doc describes the **concepts and rules** of the Goa hub — the problem it solves, the primitives, and the invariants those primitives uphold. It is deliberately not a reference for the API surface, data-model fields, or error codes; those live in the code, OpenAPI schema, and tests, which can't drift out of date the way prose does. Pending work lives in [goa-roadmap.md](goa-roadmap.md).

> v2 supersedes v1 by **generalizing the Task primitive**. v1 Tasks were 2-party and ephemeral, with a typed Message log. v2 keeps the name **Task** but generalizes the model: a task can have **N answerers** under one initiator, persists across many turns, supports **sub-tasks** for delegation and clarification, and exchanges typed **Events** instead of plain Messages.

---

## 1. Summary

Goa is a centralized hub for multi-party agent coordination and persistent task state. Agents and services register once, hold a single long-lived connection, and exchange events inside named **tasks**. A task has exactly one **initiator** — the participant solving a problem — and any number of **answerers** the initiator can target with questions. When an answerer needs help from a third party, they spawn a **sub-task** (a child task with `parent_task_id` set) where they themselves are the initiator. Privacy is achieved through task boundaries, not per-event scoping: every event in a task is visible to every participant of that task, and sub-tasks are sealed from their parents.

Goa owns: task creation and membership, event delivery, pending-question state, authentication, and observability.

State access is behind Python `Protocol`s (see [goa-core/src/goa/repos/protocols.py](../goa-core/src/goa/repos/protocols.py)) so persistence backends can be swapped without touching handlers.

## 2. Problem

The v1 hub solved 2-party request/reply with short-lived Tasks. Real workflows are wider than that:

- **Multi-agent flows are awkward as fan-out of 2-party tasks.** Today an orchestrator that consults a calculator and a summarizer creates two independent tasks and stitches results together itself. Conceptually it's one piece of work.
- **No way to bring a third party into an existing exchange.** A support agent that needs help from a payment specialist mid-conversation has no way to pull the specialist into the current context.
- **No way to model private delegation.** When an agent needs to consult a colleague to answer a customer, there is no protocol-level way to keep that consultation private from the customer-facing surface. Today it has to happen out-of-band, bypassing Goa's observability.
- **Service-fronted humans need a thread-mapping layer.** A chat service representing many users via Slack or web chat must persist its own `external_thread_id → goa_task_id` table to correlate inbound and outbound messages. Every service rebuilds this.
- **No persistent multi-turn state.** v1 tasks were short-lived units. There is no first-class "this piece of work, ongoing across many turns and multiple specialists" concept.

## 3. Goal

A developer can:

1. Register an agent or service, open a stream, and exchange events with any other participant in **under 10 minutes** — without standing up a public server.
2. Build a chat-service participant that routes a customer task through Goa **with no local persistence** of task-to-thread mapping.
3. Pull a specialist agent into a running task by simply targeting them; or, when the specialist's work should remain private, spawn a sub-task that the customer-facing surface cannot see.
4. Ask Goa "what events am I expected to reply to?" and get an answer from per-task pending state, no log scan.

## 4. Core Concepts

| Concept            | Definition |
| ------------------ | ---------- |
| **Participant**    | A registered actor. Has a type (`agent` or `service`), an ID, an API key, a description, and capability tags. |
| **Task**           | A persistent container for solving a problem. Owned by exactly one **initiator**. Append-only event log, mutable participant set, optional `parent_task_id`. The `pending_questions` view is derived from the log (§6). |
| **Event**          | A single entry in a task's log. Discriminated union keyed on a closed `event_type` enum (`question`, `answer`, `info`, `cancel_question`, `cancel_all_questions`, and Goa-emitted `participant_joined`, `child_task_created`, `parent_closed`). Carries a common envelope plus a per-type payload. |
| **Pending Question** | A stored pair `(question_event_id, target_id)` on the task. Pushed when a `question` event opens it; popped when a matching `answer` (or a `cancel_question` / `cancel_all_questions` from the initiator) closes it. The unit of "what is in-flight." |
| **Sub-task**       | A child task with `parent_task_id` set at creation. The participant who created it becomes its initiator. Sealed from the parent: parent participants cannot read the child, and child participants cannot read the parent — unless they happen to be in both. Used for delegation, clarification, and side-channels. |
| **External Ref**   | A service-supplied stable string (e.g. `slack-thread-abc123`) indexed per `(initiator_id, external_ref)`. Used for find-or-create on task creation so services hold no local mapping table. |
| **Stream**         | The long-lived SSE connection a participant holds open to receive inbound events for tasks they participate in. |

## 5. Architecture

```
   Service S (chat)              Goa Hub                 Agent C (support)        Agent P (payments)
   ────────────────              ───────                 ─────────────────        ──────────────────
       │  create task T1 ───────▶│                       │                         │
       │ ◀─────────────────────  │                       │                         │
       │  question in T1 to [C] ▶│                       │                         │
       │                         │ ── question in T1 ──▶ │                         │
       │                         │                       │                         │
       │                         │ ◀── create T2 (parent=T1) ──                    │
       │                         │ ── (T2 created) ───▶ │                         │
       │                         │                       │                         │
       │                         │ ◀── question in T2 to [P] ──                    │
       │                         │ ───────────── question in T2 ────────────────▶ │
       │                         │                       │                         │
       │                         │ ◀───────────── answer in T2 ─────────────────  │
       │                         │ ── answer in T2 ───▶ │                         │
       │                         │                       │                         │
       │                         │ ◀── answer in T1 ──── │                         │
       │ ◀── answer in T1 ────── │                       │                         │
```

`S` only sees `T1`; `P` only sees `T2`; `C` is in both (initiator of `T2`, answerer in `T1`). The customer's view contains the question and the final answer. The agent-to-agent consultation lives in `T2` and is invisible to `S`.

Properties:

- **Participants are clients of Goa, never servers.** No public endpoint required. All cross-participant traffic routes through Goa.
- **Inbound is async over SSE.** Outbound (create task, append event, upsert) is plain HTTP POST.
- **Goa is the only place that holds task state.** Participants hold no shared state with each other.
- **Privacy is achieved through task boundaries, not per-event scopes.** Within a task, every event is visible to every participant. Cross-task visibility is gated by participation. To carve out a private exchange, spawn a sub-task.
- **Pending state is materialized.** "Who owes a reply" is a stored list, not a derived query. The event log remains the canonical source of truth and the list is rebuildable from it.

## 6. Pending Questions

A task has two orthogonal dimensions: **lifecycle** (`open` vs `closed` — see §8 "Tasks have an explicit close") and **obligation** (`pending_questions`). Pending state is the unit of "what is in-flight"; status is "is the task still accepting work." A closed task may carry unanswered pending pairs (initiator gave up); an open task may have zero pending pairs (between turns).

**Push on `question`.** Appending a `question` event `E` with targets `[t1, t2, ...]` atomically pushes `(E.id, t1), (E.id, t2), ...` to the task's `pending_questions`.

**Pop on `answer`.** Appending an `answer` event from `P` referencing questions `[E1, E2, ...]` pops every pair `(Ei, P)` that is present. If a referenced question does not target `P`, the append is rejected; partial application is not allowed.

**Pop on `cancel_question`.** Appending a `cancel_question` event from the initiator referencing questions `[E1, E2, ...]` pops every pair `(Ei, _)` that is present. Only the initiator may emit `cancel_question`.

**Pop on `cancel_all_questions`.** Appending a `cancel_all_questions` event from the initiator pops every pair currently in `task.pending_questions` atomically. No-op if pending is already empty (the event is still appended; no error). Only the initiator may emit `cancel_all_questions`.

**First-answer-wins.** A second `answer` from the same target referencing the same question is appended as a normal event but does not reopen anything. The pair has a single state transition (open → closed) per `(target, question_event_id)` pair.

**Multi-target close per recipient.** A question targeting `[P, Q]` opens two pairs. `P` answering closes only `(E, P)`; `(E, Q)` stays open until `Q` answers (or the initiator retracts via `cancel_question` / clears via `cancel_all_questions`).

**Reply-with-question is not expressible in one event.** Under this model, only the initiator emits questions. An answerer who needs more context to answer either:
1. Emits an `info` event explaining what they need (the original pending pair stays open until the answerer answers or the initiator cancels), or
2. Spawns a sub-task with `parent_task_id` set, becomes its initiator, and asks whoever they need (often the original initiator) for clarification there.

**Targeting authorization.**
- Only **current participants** may target inside a task.
- A `question` event targeting a registered participant who is **not** in the task's participant list auto-joins them. Goa appends a `participant_joined` system event before delivering the targeting event; the joined participant receives both on stream replay.
- Targeting an **unknown** participant ID (not in the registry) is rejected.

**Concurrency.** Per-task locking serializes appends. Two answers from the same target to the same question arriving concurrently will commit in some order; the second one observes the pair already closed and is appended as an informational answer event (it does not reopen anything).

**Pending query (normative).** Conceptually:

```sql
SELECT q.id  AS question_event_id,
       q.task_id,
       q.from,
       q.created_at
  FROM events q
 WHERE q.event_type = 'question'
   AND :participant_id = ANY(q.payload.to)
   AND NOT EXISTS (
     SELECT 1 FROM events r
      WHERE r.task_id = q.task_id
        AND (
          (r.event_type = 'answer' AND r.from = :participant_id
            AND q.id = ANY(r.payload.answering))
          OR (r.event_type = 'cancel_question'
            AND q.id = ANY(r.payload.retracts))
          OR (r.event_type = 'cancel_all_questions'
            AND r.created_at > q.created_at)
        )
   )
```

The three arms of the `OR` reflect the three ways a pair closes: an `answer` from this specific target whose referenced questions include this one (closes per recipient), a `cancel_question` from the initiator referencing this question (closes for all targets at once), or a `cancel_all_questions` from the initiator posted after the question opened (closes every then-open pair on the task in one shot).

This SQL is the definition. At runtime, callers read pending questions from an in-process projection — a cache of this query, reconstructable from the event log on a cold start.

## 7. Task-Boundary Visibility

Visibility is determined entirely by task membership; events do not carry per-event scopes.

- **Within a task:** every event is visible to every current participant. Live SSE delivery, history replay on join, and REST reads all use the same predicate (you are in the task's participant list).
- **Across tasks:** a participant sees only tasks they are a member of. Sub-tasks are independent — being in a parent grants no read access to a child, and being in a child grants no access to the parent.
- **Late joiners** auto-joined by a targeted `question` replay the full task history (filtered only by the same membership predicate, which by definition they now satisfy).
- **System events** (`participant_joined`, `child_task_created`, `parent_closed`) follow the same rule: visible to current participants of the task they were emitted into.

For finer-grained per-event scoping within a task, see the roadmap. Most use cases that motivated per-event visibility in earlier designs (private agent coordination during a customer-facing exchange) are solved by sub-tasks.

## 8. Design Decisions

Decisions resolved during v2 design, kept here so they don't get re-litigated:

- **Session → Task.** v1's "Session" became "Task" to align with the broader (multi-party, multi-turn, sub-taskable) semantics.
- **Sub-tasks are a v2 core primitive,** not a layered feature. Private delegation needs first-class support so it routes through Goa rather than out-of-band.
- **Visibility collapses to task-boundary rule (§7).** Earlier designs proposed per-event scoping; sub-tasks express the same use cases more simply.
- **`pending_questions` is materialized on the task (§6),** not derived on every read. The event log stays canonical; the projection is a cache.
- **`event_type` is a closed enum.** Clients must reject unknown values rather than silently coercing them; adding a value is a spec-version bump.
- **`in_reply_to` is a pure threading/UI hint,** not a state-affecting field. Pending state is only affected by `question` / `answer` / `cancel_question` / `cancel_all_questions`.
- **`cancel` was split into `cancel_question` (per-question retract) and `cancel_all_questions` (clear all open)** so the cancel scope is explicit on the wire rather than overloaded.
- **Event wire shape is a discriminated union,** not flat fields with per-type validation. Each variant's payload contains only the fields meaningful for that type — `payload.to` on `question`, `payload.answering` on `answer`, `payload.retracts` on `cancel_question`. Schema validation expresses the contract directly; no field is "rejected when populated on the wrong type" because the field doesn't exist on the wrong type.
- **`event_type` is required on append.** No shape-inference from which fields the caller populated.
- **`child_task_created` is always emitted** in the parent task when a sub-task is created. No opt-out flag — observability is not optional.
- **`participant.access_policy` ships as a field with default `"public"` and no enforcement.** Reserved so the registry schema is forward-compatible when ACL enforcement lands; until then, no traffic changes.
- **Task creation does not nest an `opening_event`.** Task creation and the first event are separate calls. This was considered and rejected — collapsing them would have made task-level and event-level fields collide in one body.
- **Event append is idempotent via `client_event_id`.** Every inbound event carries an optional `client_event_id` (UUID) on the common envelope. Two appends with the same `(task_id, from, client_event_id)` resolve to a single persisted event — the second returns the originally-persisted record without re-mutating pending state or re-fanning out on SSE. Opt-in (no SDK auto-generation in v2); the key is opaque to Goa beyond uniqueness, and clients own the keyspace (no body-hash conflict detection — reusing a key for a different intent is a client bug). System events (`from = null`) never carry a key.
- **Tasks have an explicit close.** Initiator calls `POST /tasks/{id}/close` to transition a task from `open` to `closed`. Subsequent event appends are rejected with `409 invalid_state`. The `(initiator_id, external_ref)` slot is released so re-`upsert` with the same external_ref creates a fresh task; the closed-task row keeps its `external_ref` for audit. Open child tasks receive a `parent_closed` system event but are not cascade-closed — they remain independent per §7. Close is idempotent (closing a closed task is a no-op). No idle-timeout sweeper in this release; cleanup is initiator-driven.
