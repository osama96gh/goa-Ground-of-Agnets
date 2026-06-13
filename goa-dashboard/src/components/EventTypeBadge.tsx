import type { EventType } from "@/lib/types";
import { cn } from "@/lib/utils";

// Token-aware palette — each pill uses a tinted background that reads in both
// light and dark themes (color/15 alpha over the themed surface).
const STYLES: Record<EventType, string> = {
  question: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  answer: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  info: "bg-muted text-muted-foreground",
  cancel_question: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  cancel_all_questions: "bg-amber-500/25 text-amber-700 dark:text-amber-300",
  participant_joined: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  child_task_created: "bg-indigo-500/15 text-indigo-600 dark:text-indigo-400",
  parent_closed: "bg-rose-500/15 text-rose-600 dark:text-rose-400",
};

export function EventTypeBadge({ type }: { type: EventType }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        STYLES[type],
      )}
    >
      {type}
    </span>
  );
}
