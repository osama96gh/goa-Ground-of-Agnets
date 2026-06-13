import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listAdminParticipants } from "../api/admin";
import { EventCard } from "../components/EventCard";
import type { TimelineEntry } from "../components/Layout";
import type { Participant } from "../lib/types";

export function TimelinePage() {
  const navigate = useNavigate();

  // Read-only: the firehose subscription in Layout.tsx populates this cache.
  const { data: entries } = useQuery<TimelineEntry[]>({
    queryKey: ["timeline"],
    initialData: [],
    queryFn: () => Promise.resolve([]),
    // The firehose pushes; React Query never re-fetches this.
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
      <div>
        <h1 className="text-2xl font-semibold">Timeline</h1>
        <p className="text-sm text-slate-500">
          Live admin firehose — every event in the system, newest first.
          Buffer keeps the last 500 events.
        </p>
      </div>
      {entries.length === 0 ? (
        <div className="rounded-md border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">
          Waiting for events. Trigger one with{" "}
          <code className="rounded bg-slate-100 px-1">make example-chat</code>.
        </div>
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
