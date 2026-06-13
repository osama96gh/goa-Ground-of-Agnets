// Wire types — mirror goa-sdk/src/goa_sdk/{models,events}.py.
// When the SDK shape changes, mirror it here too.

export type ParticipantType = "agent" | "service";
export type AccessPolicy = "public" | "private";

export interface Participant {
  id: string;
  type: ParticipantType;
  name: string;
  description: string;
  capabilities: string[];
  access_policy: AccessPolicy;
  api_key_hash: string;
  created_at: string;
}

export type PendingPair = [string, string]; // [question_event_id, target_id]

// pending_questions is a derived view, not part of the persisted Task. Read
// it from `GetTaskResponse.pending_questions` (detail endpoint) or
// `TaskListItem.pending_questions` (list endpoints).
// `TaskSummary.pending_questions` still rides on SSE stream frames.
export type TaskStatus = "open" | "closed";

export interface Task {
  id: string;
  initiator_id: string;
  parent_task_id: string | null;
  status: TaskStatus;
  participants: string[];
  subject: string;
  external_ref: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  last_activity_at: string;
}

// List-endpoint composite — `GET /tasks`, `GET /admin/tasks`,
// `GET /tasks/{id}/children` all return arrays of these.
export interface TaskListItem {
  task: Task;
  pending_questions: PendingPair[];
}

export interface TaskSummary {
  id: string;
  subject: string;
  participants: string[];
  parent_task_id: string | null;
  pending_questions: PendingPair[];
  last_activity_at: string;
}

// Event discriminated union — matches §6.3.

export interface Attachment {
  blob_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
}

export interface Content {
  text?: string | null;
  data?: Record<string, unknown> | null;
  attachments?: Attachment[];
}

export interface EventEnvelope {
  id: string;
  task_id: string;
  from: string | null;
  content: Content;
  in_reply_to: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export type QuestionEvent = EventEnvelope & {
  event_type: "question";
  payload: { to: string[] };
};
export type AnswerEvent = EventEnvelope & {
  event_type: "answer";
  payload: { answering: string[] };
};
export type InfoEvent = EventEnvelope & {
  event_type: "info";
  payload: Record<string, never>;
};
export type CancelQuestionEvent = EventEnvelope & {
  event_type: "cancel_question";
  payload: { retracts: string[] };
};
export type CancelAllQuestionsEvent = EventEnvelope & {
  event_type: "cancel_all_questions";
  payload: Record<string, never>;
};
export type ParticipantJoinedEvent = EventEnvelope & {
  event_type: "participant_joined";
  payload: { participant_id: string };
};
export type ChildTaskCreatedEvent = EventEnvelope & {
  event_type: "child_task_created";
  payload: { task_id: string; spawned_by: string; subject?: string | null };
};
export type ParentClosedEvent = EventEnvelope & {
  event_type: "parent_closed";
  payload: { task_id: string };
};

export type Event =
  | QuestionEvent
  | AnswerEvent
  | InfoEvent
  | CancelQuestionEvent
  | CancelAllQuestionsEvent
  | ParticipantJoinedEvent
  | ChildTaskCreatedEvent
  | ParentClosedEvent;

export type EventType = Event["event_type"];

// Stream frame (§9.3) — the `event`-named SSE frame's `data` shape.
export interface StreamEventFrame {
  task_id: string;
  event: Event;
  task: TaskSummary;
}

export interface StreamGapData {
  from_id: number;
  to_id: number;
}

// Bootstrap-time create body shape (§9.1).
export interface CreateParticipantBody {
  type: ParticipantType;
  name: string;
  description?: string;
  capabilities?: string[];
}

export interface CreateParticipantResponse {
  participant: Participant;
  api_key: string;
}

export interface AdminCreateParticipantBody {
  type: ParticipantType;
  name: string;
  description?: string;
  capabilities?: string[];
}

export interface AdminCreateParticipantResponse {
  participant: Participant;
  api_key: string;
}

export interface AdminUpdateParticipantBody {
  name?: string;
  description?: string;
  capabilities?: string[];
}

// Keyset-paginated admin task list — `GET /admin/tasks`.
export interface TaskPage {
  tasks: TaskListItem[];
  next_cursor: string | null;
}

// Aggregate metrics — `GET /admin/stats`.
export interface AdminStats {
  totals: {
    tasks: number;
    tasks_open: number;
    tasks_closed: number;
    participants: number;
    participants_agent: number;
    participants_service: number;
    pending_questions: number;
    events_total: number;
  };
  events_today: number;
  event_volume: { date: string; count: number }[];
  tasks_by_status: { open: number; closed: number };
  recent_activity: {
    task_id: string;
    subject: string;
    status: TaskStatus;
    last_activity_at: string;
    pending_count: number;
  }[];
  pending_backlog: {
    task_id: string;
    subject: string;
    pending_count: number;
    oldest_pending_at: string | null;
  }[];
}

// Agent-private memory entry (§9). Owner-scoped; the dashboard reads it
// per-participant via the admin token (read-only).
export interface MemoryEntry {
  id: string;
  owner_id: string;
  key: string;
  value: unknown; // any JSON value
  tags: string[];
  created_at: string;
  updated_at: string;
}
