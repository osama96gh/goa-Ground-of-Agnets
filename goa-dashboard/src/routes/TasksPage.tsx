import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listAdminParticipants, listAdminTasks } from "../api/admin";
import { ParticipantBadge } from "../components/ParticipantBadge";
import type { Participant } from "../lib/types";

type PendingFilter = "all" | "open" | "closed";

export function TasksPage() {
  const [pending, setPending] = useState<PendingFilter>("all");
  const [showAll, setShowAll] = useState(false);

  const { data: tasks, isLoading } = useQuery({
    queryKey: ["admin", "tasks", pending, showAll],
    queryFn: () =>
      listAdminTasks({
        has_pending:
          pending === "all" ? undefined : pending === "open" ? true : false,
        // null means "top-level only"; undefined means "show every task".
        parent_id: showAll ? undefined : null,
      }),
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
          <h1 className="text-2xl font-semibold">Tasks</h1>
          <p className="text-sm text-slate-500">
            Read-only listing across all tasks.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-slate-200 bg-white p-3 text-sm">
        <label className="flex items-center gap-2">
          <span className="text-slate-600">Pending</span>
          <select
            value={pending}
            onChange={(e) => setPending(e.target.value as PendingFilter)}
            className="rounded border border-slate-300 px-2 py-1"
          >
            <option value="all">all</option>
            <option value="open">open only</option>
            <option value="closed">no pending</option>
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => setShowAll(e.target.checked)}
          />
          <span className="text-slate-600">Include sub-tasks (off → top-level only)</span>
        </label>
      </div>
      {isLoading && <div className="text-sm text-slate-500">Loading…</div>}
      <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Subject</th>
              <th className="px-3 py-2">Initiator</th>
              <th className="px-3 py-2">Participants</th>
              <th className="px-3 py-2">Pending</th>
              <th className="px-3 py-2">Parent</th>
              <th className="px-3 py-2">Last activity</th>
            </tr>
          </thead>
          <tbody>
            {/* Stages 2+3: items are {task, pending_questions}. */}
            {(tasks ?? []).map((item) => {
              const t = item.task;
              return (
              <tr key={t.id} className="border-t border-slate-100">
                <td className="px-3 py-2">
                  <Link
                    to={`/tasks/${t.id}`}
                    className="text-blue-600 hover:underline"
                  >
                    {t.subject || "(no subject)"}
                  </Link>
                  <div className="font-mono text-xs text-slate-400">
                    {t.id.slice(0, 8)}
                    {t.external_ref && (
                      <span className="ml-2 rounded bg-slate-100 px-1 text-slate-600">
                        ref: {t.external_ref}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <ParticipantBadge
                    participant={byId.get(t.initiator_id)}
                    fallbackId={t.initiator_id}
                  />
                </td>
                <td className="px-3 py-2 text-slate-600">{t.participants.length}</td>
                <td className="px-3 py-2">
                  {item.pending_questions.length === 0 ? (
                    <span className="text-slate-400">0</span>
                  ) : (
                    <span className="font-medium text-amber-700">
                      {item.pending_questions.length}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">
                  {t.parent_task_id ? (
                    <Link
                      to={`/tasks/${t.parent_task_id}`}
                      className="text-blue-600 hover:underline"
                    >
                      {t.parent_task_id.slice(0, 8)}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {new Date(t.last_activity_at).toLocaleTimeString()}
                </td>
              </tr>
              );
            })}
            {tasks && tasks.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                  No tasks yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
