# Goa — Roadmap

Pending work on the Goa hub. The spec at [goa.md](goa.md) describes the system as it is; this file lists what it isn't yet. Items here are wishlist, not contract — move shipped items out (into the spec or out of this file) rather than annotating them as "done."

For tracked work, prefer GitHub issues; this file is for items not yet broken out.

| Topic | Notes |
| ----- | ----- |
| Idle-timeout task close | Background sweeper that calls the existing `close_task` on tasks whose `last_activity_at` is older than a configurable threshold (e.g. `GOA_IDLE_CLOSE_AFTER=72h`). Explicit initiator-driven close already ships (§8); this adds automated cleanup. |
| Per-event visibility within a task | Fine-grained scoping inside a single task (announce-to-subset, agent-only sub-conversations without spawning a child). Currently all such use cases are expressible as sub-tasks; this would be an ergonomics layer if it proves needed. |
| Audit-participant role | Read-only join for compliance/audit, with cross-tree access (parent + descendant children). Replaces the current "spawn a sub-task" workaround for shadow-style scenarios. |
| Provenance metadata | Convention for `metadata.derived_from: [task_id, ...]` on answers so audit/replay tools can traverse delegation chains without granting read access into children. |
| Descendant-aggregated billing/SLA | Rollup endpoints for total cost / latency across a task tree. |
| Pending-question-closed system events | Push notifications when a pending pair closes, for participants that want to react without polling `pending()`. Additive. |
| SLA timers / escalation | Per-question `expires_at` plus a sweeper that emits a system event when a pair passes its deadline. Builds on the close-event mechanism above. |
| Sync sub-call helper | Blocking `POST /tasks/{id}/ask` (or equivalent) so an agent can target another participant and wait for the reply, while still routing through Goa for observability. Currently expressible as `event` + `await pending()` polling in the SDK. |
| Per-event-type authorization tightening | Restrict who may emit `info` (e.g. block answerers from emitting after their pending pair closes); restrict `participant_joined` emission to Goa. Strictly tightening — current clients keep working. |
| Per-participant access policy enforcement | Honor `participant.access_policy`. When `private`, targeting that participant requires a per-participant ACL match (initial proposal: a `targetable_by` list of participant IDs, plus `targetable_by_capability` tag set). Field ships today with default `"public"` and no enforcement; future cutover turns enforcement on with a one-release deprecation window. |
| Embedding-based discovery | Bolt onto participant search — no contract change. |
| Webhook delivery | Same delivery module would choose transport per participant. |
| A2A protocol bridge | Speak the standard A2A protocol on Goa's perimeter so self-hosted agents and Goa-hosted participants interoperate without lock-in. |
| Cost / billing surface | Aggregate token usage per task plus per-event token counts; needs aggregation + UI. |
| Rate limiting & quotas | Per-participant and per-task. |
| Audit log export | Capture `initiator_auth_context` on tasks; combined with the event log, reconstructs everything; needs an export path. |
| Multi-replica persistence | `LISTEN/NOTIFY`-style projection invalidation and cross-replica SSE fanout. Shipped adapters (in-memory, SQLite) are single-replica only. |
| Schema-formalized `external_ref` namespacing | Convention like `slack:thread:abc123` or a separate `external_namespace` column to prevent accidental collisions across services. |
