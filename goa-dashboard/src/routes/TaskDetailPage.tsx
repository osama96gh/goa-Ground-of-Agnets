import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, Paperclip } from "lucide-react";
import {
  closeAdminTask,
  getAdminTask,
  listAdminParticipants,
  listAdminTasks,
} from "@/api/admin";
import type { Attachment, Participant, TaskListItem } from "@/lib/types";
import { formatDateTime, shortId } from "@/lib/format";
import { EventCard } from "@/components/EventCard";
import { ParticipantBadge } from "@/components/ParticipantBadge";
import { PendingPairList } from "@/components/PendingPairList";
import { SubTaskTree } from "@/components/SubTaskTree";
import { AttachmentList } from "@/components/AttachmentList";
import { EmptyState } from "@/components/EmptyState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const qc = useQueryClient();

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

  const { data: children } = useQuery({
    queryKey: ["admin", "tasks", "children", taskId],
    queryFn: () => listAdminTasks({ parent_id: taskId }),
    enabled: Boolean(taskId),
  });
  const childrenByParent = new Map<string, TaskListItem[]>();
  childrenByParent.set(taskId, children ?? []);

  const closeMutation = useMutation({
    mutationFn: () => closeAdminTask(taskId),
    onSuccess: () => {
      toast.success("Task closed.");
      qc.invalidateQueries({ queryKey: ["admin", "task", taskId] });
      qc.invalidateQueries({ queryKey: ["admin", "tasks"] });
      qc.invalidateQueries({ queryKey: ["admin", "stats"] });
    },
  });

  // Blobs tab derives from event attachments — no dedicated endpoint exists,
  // and every attachment already rides on the task's event log.
  const attachments = useMemo<Attachment[]>(() => {
    const seen = new Map<string, Attachment>();
    for (const ev of data?.events ?? []) {
      for (const att of ev.content?.attachments ?? []) {
        seen.set(att.blob_id, att);
      }
    }
    return [...seen.values()];
  }, [data]);

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (error) {
    return (
      <EmptyState
        title="Couldn't load task"
        description={(error as Error).message}
      />
    );
  }
  if (!data) return null;

  const { task, pending_questions, events } = data;

  return (
    <div className="space-y-6">
      <div>
        <Link
          to="/tasks"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> All tasks
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {task.subject || "(no subject)"}
            </h1>
            <Badge variant={task.status === "open" ? "success" : "secondary"}>
              {task.status}
            </Badge>
          </div>
          {task.status === "open" && (
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="destructive">Close task</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Close this task?</DialogTitle>
                  <DialogDescription>
                    Closing transitions the task to <code>closed</code>, frees
                    its external ref, and notifies open child tasks. This can't
                    be undone from the dashboard.
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <DialogClose asChild>
                    <Button variant="outline">Cancel</Button>
                  </DialogClose>
                  <DialogClose asChild>
                    <Button
                      variant="destructive"
                      onClick={() => closeMutation.mutate()}
                      disabled={closeMutation.isPending}
                    >
                      Close task
                    </Button>
                  </DialogClose>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
        </div>
        <div className="mt-1 font-mono text-xs text-muted-foreground">
          {task.id}
        </div>
      </div>

      <Card>
        <CardContent className="grid grid-cols-2 gap-4 p-4 text-sm md:grid-cols-4">
          <Meta label="Initiator">
            <ParticipantBadge
              participant={byId.get(task.initiator_id)}
              fallbackId={task.initiator_id}
            />
          </Meta>
          <Meta label="Parent task">
            {task.parent_task_id ? (
              <Link
                to={`/tasks/${task.parent_task_id}`}
                className="font-mono text-xs text-primary hover:underline"
              >
                {shortId(task.parent_task_id)}
              </Link>
            ) : (
              <span className="text-muted-foreground">—</span>
            )}
          </Meta>
          <Meta label="External ref">
            <span className="font-mono text-xs">
              {task.external_ref || (
                <span className="text-muted-foreground">—</span>
              )}
            </span>
          </Meta>
          <Meta label="Created">
            <span className="text-xs">{formatDateTime(task.created_at)}</span>
          </Meta>
        </CardContent>
      </Card>

      <Tabs defaultValue="events">
        <TabsList className="flex max-w-full justify-start overflow-x-auto">
          <TabsTrigger value="events">Events ({events.length})</TabsTrigger>
          <TabsTrigger value="pending">
            Pending ({pending_questions.length})
          </TabsTrigger>
          <TabsTrigger value="subtasks">
            Sub-tasks ({children?.length ?? 0})
          </TabsTrigger>
          <TabsTrigger value="blobs">
            Attachments ({attachments.length})
          </TabsTrigger>
          <TabsTrigger value="participants">
            Members ({task.participants.length})
          </TabsTrigger>
        </TabsList>

        <TabsContent value="events" className="space-y-2">
          {events.length === 0 ? (
            <EmptyState title="No events yet" />
          ) : (
            events.map((ev) => (
              <EventCard key={ev.id} event={ev} participants={byId} />
            ))
          )}
        </TabsContent>

        <TabsContent value="pending">
          <PendingPairList pending={pending_questions} participants={byId} />
        </TabsContent>

        <TabsContent value="subtasks">
          {(children ?? []).length === 0 ? (
            <EmptyState title="No sub-tasks" />
          ) : (
            <Card>
              <CardContent className="p-4">
                <SubTaskTree
                  children={children ?? []}
                  childrenByParent={childrenByParent}
                />
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="blobs">
          {attachments.length === 0 ? (
            <EmptyState icon={Paperclip} title="No attachments on this task" />
          ) : (
            <AttachmentList attachments={attachments} />
          )}
        </TabsContent>

        <TabsContent value="participants">
          <div className="flex flex-wrap gap-2">
            {task.participants.map((p) => (
              <ParticipantBadge key={p} participant={byId.get(p)} fallbackId={p} />
            ))}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Meta({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1">{children}</div>
    </div>
  );
}
