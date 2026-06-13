import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getAdminTask, listAdminParticipants, listAdminTasks } from "../api/admin";
import { EventCard } from "../components/EventCard";
import { ParticipantBadge } from "../components/ParticipantBadge";
import { PendingPairList } from "../components/PendingPairList";
import { SubTaskTree } from "../components/SubTaskTree";
import type { Participant, TaskListItem } from "../lib/types";

export function TaskDetailPage() {
  const { taskId = "" } = useParams();

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "task", taskId],
    queryFn: () => getAdminTask(taskId),
    enabled: Boolean(taskId),
  });

  const { data: participants } = useQuery<Participant[]>({
    queryKey: ["admin", "participants"],
    queryFn: () => listAdminParticipants(),
  });
  const byId = new Map<string, Participant>(
    (participants ?? []).map((p) => [p.id, p]),
  );

  // Children — single hop. Deeper trees are loaded as the user navigates in.
  const { data: children } = useQuery({
    queryKey: ["admin", "tasks", "children", taskId],
    queryFn: () => listAdminTasks({ parent_id: taskId }),
    enabled: Boolean(taskId),
  });
  const childrenByParent = new Map<string, TaskListItem[]>();
  childrenByParent.set(taskId, children ?? []);

  if (isLoading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (error)
    return (
      <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
        {(error as Error).message}
      </div>
    );
  if (!data) return null;

  // Stages 2+3: pending_questions is a sibling of task in the response.
  const { task, pending_questions, events } = data;
  return (
    <div className="space-y-6">
      <div>
        <Link to="/tasks" className="text-sm text-blue-600 hover:underline">
          ← All tasks
        </Link>
        <h1 className="mt-1 text-2xl font-semibold">
          {task.subject || "(no subject)"}
        </h1>
        <div className="mt-1 text-xs font-mono text-slate-500">{task.id}</div>
      </div>

      <section className="grid grid-cols-2 gap-3 rounded-md border border-slate-200 bg-white p-3 text-sm">
        <div>
          <span className="text-xs text-slate-500">Initiator</span>
          <div>
            <ParticipantBadge
              participant={byId.get(task.initiator_id)}
              fallbackId={task.initiator_id}
            />
          </div>
        </div>
        <div>
          <span className="text-xs text-slate-500">Parent task</span>
          <div>
            {task.parent_task_id ? (
              <Link
                to={`/tasks/${task.parent_task_id}`}
                className="font-mono text-xs text-blue-600 hover:underline"
              >
                {task.parent_task_id}
              </Link>
            ) : (
              <span className="text-slate-400">—</span>
            )}
          </div>
        </div>
        <div>
          <span className="text-xs text-slate-500">External ref</span>
          <div className="font-mono text-xs">
            {task.external_ref ? task.external_ref : <span className="text-slate-400">—</span>}
          </div>
        </div>
        <div>
          <span className="text-xs text-slate-500">Participants</span>
          <div className="flex flex-wrap gap-1">
            {task.participants.map((p) => (
              <ParticipantBadge key={p} participant={byId.get(p)} fallbackId={p} />
            ))}
          </div>
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-medium">Pending questions</h2>
        <PendingPairList pending={pending_questions} participants={byId} />
      </section>

      {(children ?? []).length > 0 && (
        <section>
          <h2 className="mb-2 text-lg font-medium">Sub-tasks</h2>
          <div className="rounded-md border border-slate-200 bg-white p-3">
            <SubTaskTree
              children={children ?? []}
              childrenByParent={childrenByParent}
            />
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-2 text-lg font-medium">Events ({events.length})</h2>
        <div className="space-y-2">
          {events.map((ev) => (
            <EventCard key={ev.id} event={ev} participants={byId} />
          ))}
        </div>
      </section>
    </div>
  );
}
