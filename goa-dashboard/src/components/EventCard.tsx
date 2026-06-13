import type { Event, Participant } from "@/lib/types";
import { shortId } from "@/lib/format";
import { AttachmentList } from "./AttachmentList";
import { EventTypeBadge } from "./EventTypeBadge";
import { ParticipantBadge } from "./ParticipantBadge";

interface Props {
  event: Event;
  participants: Map<string, Participant>;
  taskSubject?: string;
  onTaskClick?: (taskId: string) => void;
}

export function EventCard({
  event,
  participants,
  taskSubject,
  onTaskClick,
}: Props) {
  const from = event.from ? participants.get(event.from) : undefined;
  const created = new Date(event.created_at).toLocaleTimeString();
  return (
    <div className="rounded-lg border bg-card p-3 text-sm shadow-sm">
      <div className="mb-1 flex items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <EventTypeBadge type={event.event_type} />
          {event.from ? (
            <ParticipantBadge participant={from} fallbackId={event.from} />
          ) : (
            <span className="text-xs text-muted-foreground">system</span>
          )}
          {taskSubject !== undefined && (
            <button
              className="text-xs text-muted-foreground hover:text-foreground hover:underline"
              onClick={() => onTaskClick?.(event.task_id)}
            >
              · {taskSubject || "(no subject)"}
            </button>
          )}
        </div>
        <span className="shrink-0 font-mono text-xs text-muted-foreground">
          {created}
        </span>
      </div>
      <PayloadView event={event} participants={participants} />
      {event.content?.text && (
        <div className="mt-1 whitespace-pre-wrap text-foreground">
          {event.content.text}
        </div>
      )}
      {event.content?.data && Object.keys(event.content.data).length > 0 && (
        <pre className="mt-1 overflow-x-auto rounded-md bg-muted p-2 text-xs">
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
        <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
          to:
          {event.payload.to.map((id) => (
            <ParticipantBadge
              key={id}
              participant={participants.get(id)}
              fallbackId={id}
            />
          ))}
        </div>
      );
    case "answer":
      return (
        <div className="font-mono text-xs text-muted-foreground">
          answering: {event.payload.answering.map((id) => shortId(id)).join(", ")}
        </div>
      );
    case "cancel_question":
      return (
        <div className="font-mono text-xs text-muted-foreground">
          retracts: {event.payload.retracts.map((id) => shortId(id)).join(", ")}
        </div>
      );
    case "participant_joined":
      return (
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          joined:
          <ParticipantBadge
            participant={participants.get(event.payload.participant_id)}
            fallbackId={event.payload.participant_id}
          />
        </div>
      );
    case "child_task_created":
      return (
        <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
          spawned by
          <ParticipantBadge
            participant={participants.get(event.payload.spawned_by)}
            fallbackId={event.payload.spawned_by}
          />
          {" · child "}
          <span className="font-mono">{shortId(event.payload.task_id)}</span>
          {event.payload.subject ? ` · "${event.payload.subject}"` : ""}
        </div>
      );
    case "parent_closed":
      return (
        <div className="text-xs text-muted-foreground">
          parent <span className="font-mono">{shortId(event.payload.task_id)}</span>{" "}
          closed
        </div>
      );
    default:
      return null;
  }
}
