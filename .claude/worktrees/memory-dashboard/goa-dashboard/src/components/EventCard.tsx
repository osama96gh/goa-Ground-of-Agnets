import type { Event, Participant } from "../lib/types";
import { AttachmentList } from "./AttachmentList";
import { EventTypeBadge } from "./EventTypeBadge";
import { ParticipantBadge } from "./ParticipantBadge";

interface Props {
  event: Event;
  participants: Map<string, Participant>;
  taskSubject?: string;
  onTaskClick?: (taskId: string) => void;
}

export function EventCard({ event, participants, taskSubject, onTaskClick }: Props) {
  const from = event.from ? participants.get(event.from) : undefined;
  const created = new Date(event.created_at).toLocaleTimeString();
  return (
    <div className="rounded-md border border-slate-200 bg-white p-3 text-sm">
      <div className="mb-1 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <EventTypeBadge type={event.event_type} />
          <ParticipantBadge participant={from} fallbackId={event.from} />
          {taskSubject && (
            <button
              className="text-xs text-slate-500 hover:underline"
              onClick={() => onTaskClick?.(event.task_id)}
            >
              · {taskSubject || "(no subject)"}
            </button>
          )}
        </div>
        <span className="font-mono text-xs text-slate-400">{created}</span>
      </div>
      <PayloadView event={event} participants={participants} />
      {event.content?.text && (
        <div className="mt-1 whitespace-pre-wrap text-slate-800">{event.content.text}</div>
      )}
      {event.content?.data && Object.keys(event.content.data).length > 0 && (
        <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 text-xs">
          {JSON.stringify(event.content.data, null, 2)}
        </pre>
      )}
      {event.content?.attachments && event.content.attachments.length > 0 && (
        <AttachmentList attachments={event.content.attachments} />
      )}
    </div>
  );
}

function PayloadView({
  event,
  participants,
}: {
  event: Event;
  participants: Map<string, Participant>;
}) {
  switch (event.event_type) {
    case "question":
      return (
        <div className="text-xs text-slate-500">
          to:{" "}
          {event.payload.to.map((id, i) => (
            <span key={id}>
              {i > 0 && ", "}
              <ParticipantBadge participant={participants.get(id)} fallbackId={id} />
            </span>
          ))}
        </div>
      );
    case "answer":
      return (
        <div className="font-mono text-xs text-slate-500">
          answering: {event.payload.answering.map((id) => id.slice(0, 8)).join(", ")}
        </div>
      );
    case "cancel_question":
      return (
        <div className="font-mono text-xs text-slate-500">
          retracts: {event.payload.retracts.map((id) => id.slice(0, 8)).join(", ")}
        </div>
      );
    case "participant_joined":
      return (
        <div className="text-xs text-slate-500">
          joined:{" "}
          <ParticipantBadge
            participant={participants.get(event.payload.participant_id)}
            fallbackId={event.payload.participant_id}
          />
        </div>
      );
    case "child_task_created":
      return (
        <div className="text-xs text-slate-500">
          spawned by{" "}
          <ParticipantBadge
            participant={participants.get(event.payload.spawned_by)}
            fallbackId={event.payload.spawned_by}
          />
          {" · child "}
          <span className="font-mono">{event.payload.task_id.slice(0, 8)}</span>
          {event.payload.subject ? ` · "${event.payload.subject}"` : ""}
        </div>
      );
    case "parent_closed":
      return (
        <div className="text-xs text-slate-500">
          parent <span className="font-mono">{event.payload.task_id.slice(0, 8)}</span>{" "}
          closed
        </div>
      );
    default:
      return null;
  }
}
