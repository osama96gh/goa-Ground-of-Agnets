import type { EventType } from "../lib/types";

const STYLES: Record<EventType, string> = {
  question: "bg-blue-100 text-blue-700",
  answer: "bg-green-100 text-green-700",
  info: "bg-slate-100 text-slate-700",
  cancel_question: "bg-amber-100 text-amber-700",
  cancel_all_questions: "bg-amber-200 text-amber-800",
  participant_joined: "bg-purple-100 text-purple-700",
  child_task_created: "bg-indigo-100 text-indigo-700",
  parent_closed: "bg-rose-100 text-rose-700",
};

export function EventTypeBadge({ type }: { type: EventType }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[type]}`}
    >
      {type}
    </span>
  );
}
