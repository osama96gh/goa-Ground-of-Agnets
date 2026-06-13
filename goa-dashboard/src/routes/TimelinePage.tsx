import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Radio } from "lucide-react";
import { listAdminParticipants } from "@/api/admin";
import { EventCard } from "@/components/EventCard";
import { EmptyState } from "@/components/EmptyState";
import type { TimelineEntry } from "@/components/Layout";
import type { Participant } from "@/lib/types";

export function TimelinePage() {
  const navigate = useNavigate();

  // Read-only: the firehose subscription in Layout.tsx populates this cache.
  const { data: entries } = useQuery<TimelineEntry[]>({
    queryKey: ["timeline"],
    initialData: [],
    queryFn: () => Promise.resolve([]),
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });

  const { data: participants } = useQuery<Participant[]>({
    queryKey: ["admin", "participants"],
    queryFn: () => listAdminParticipants(),
  });
  const byId = new Map<string, Participant>(
    (participants ?? []).map((p) => [p.id, p]),
  );

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Timeline</h1>
          <p className="text-sm text-muted-foreground">
            Live firehose — every event across the system, newest first (last
            500 buffered).
          </p>
        </div>
        {entries.length > 0 && (
          <span className="text-sm text-muted-foreground">
            {entries.length} event{entries.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {entries.length === 0 ? (
        <EmptyState
          icon={Radio}
          title="Waiting for events"
          description="Trigger one with `make example-chat`, or interact with any task — it will appear here instantly."
        />
      ) : (
        <div className="space-y-2">
          {entries.map((entry, idx) => (
            <EventCard
              key={`${entry.frame.event.id}-${idx}`}
              event={entry.frame.event}
              participants={byId}
              taskSubject={entry.frame.task.subject}
              onTaskClick={(taskId) => navigate(`/tasks/${taskId}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
