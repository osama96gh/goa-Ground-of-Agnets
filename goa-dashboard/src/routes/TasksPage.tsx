import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { ListTodo, MoreHorizontal, Search, X } from "lucide-react";
import { closeAdminTask, listAdminParticipants, listAdminTaskPage } from "@/api/admin";
import type { Participant, TaskStatus } from "@/lib/types";
import { formatRelative, shortId } from "@/lib/format";
import { ParticipantBadge } from "@/components/ParticipantBadge";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type StatusFilter = "all" | TaskStatus;
type PendingFilter = "all" | "pending" | "clear";

const PAGE_SIZE = 25;

export function TasksPage() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<StatusFilter>("all");
  const [pendingFilter, setPendingFilter] = useState<PendingFilter>("all");
  const [includeSubtasks, setIncludeSubtasks] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Debounce the subject search so each keystroke doesn't fire a request.
  useEffect(() => {
    const id = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(id);
  }, [searchInput]);

  const filters = {
    status: status === "all" ? undefined : status,
    has_pending:
      pendingFilter === "all" ? undefined : pendingFilter === "pending",
    parent_id: includeSubtasks ? undefined : (null as null | undefined),
    q: search || undefined,
  };

  const {
    data,
    isLoading,
    isError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["admin", "tasks", "list", filters],
    queryFn: ({ pageParam }) =>
      listAdminTaskPage({ ...filters, limit: PAGE_SIZE, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const items = useMemo(
    () => data?.pages.flatMap((p) => p.tasks) ?? [],
    [data],
  );

  const { data: participants } = useQuery<Participant[]>({
    queryKey: ["admin", "participants"],
    queryFn: () => listAdminParticipants(),
  });
  const byId = new Map<string, Participant>(
    (participants ?? []).map((p) => [p.id, p]),
  );

  const closeMutation = useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map(closeAdminTask)),
    onSuccess: (_res, ids) => {
      toast.success(`Closed ${ids.length} task${ids.length === 1 ? "" : "s"}.`);
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["admin", "tasks"] });
      qc.invalidateQueries({ queryKey: ["admin", "stats"] });
    },
  });

  const openSelected = items
    .filter((i) => selected.has(i.task.id) && i.task.status === "open")
    .map((i) => i.task.id);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const visibleOpenIds = items
    .filter((i) => i.task.status === "open")
    .map((i) => i.task.id);
  const allOpenSelected =
    visibleOpenIds.length > 0 && visibleOpenIds.every((id) => selected.has(id));

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Tasks</h1>
          <p className="text-sm text-muted-foreground">
            Every task across the hub — filter, search, and close.
          </p>
        </div>
        {openSelected.length > 0 && (
          <Button
            variant="destructive"
            onClick={() => closeMutation.mutate(openSelected)}
            disabled={closeMutation.isPending}
          >
            Close {openSelected.length} selected
          </Button>
        )}
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search subjects…"
            className="pl-8"
          />
          {searchInput && (
            <button
              onClick={() => setSearchInput("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <Select value={status} onValueChange={(v) => setStatus(v as StatusFilter)}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="closed">Closed</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={pendingFilter}
          onValueChange={(v) => setPendingFilter(v as PendingFilter)}
        >
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Pending" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Any pending</SelectItem>
            <SelectItem value="pending">Has pending</SelectItem>
            <SelectItem value="clear">No pending</SelectItem>
          </SelectContent>
        </Select>
        <label className="flex items-center gap-2 px-2 text-sm text-muted-foreground">
          <Checkbox
            checked={includeSubtasks}
            onCheckedChange={(c) => setIncludeSubtasks(Boolean(c))}
          />
          Include sub-tasks
        </label>
      </div>

      {isError ? (
        <EmptyState title="Couldn't load tasks" description="The request failed." />
      ) : isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={ListTodo}
          title="No matching tasks"
          description="Adjust the filters above, or trigger activity with `make example-chat`."
        />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <Checkbox
                    checked={allOpenSelected}
                    onCheckedChange={(c) =>
                      setSelected(c ? new Set(visibleOpenIds) : new Set())
                    }
                    aria-label="Select all open tasks"
                  />
                </TableHead>
                <TableHead>Subject</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Initiator</TableHead>
                <TableHead className="text-center">Members</TableHead>
                <TableHead className="text-center">Pending</TableHead>
                <TableHead>Last activity</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item) => {
                const t = item.task;
                const pending = item.pending_questions.length;
                return (
                  <TableRow
                    key={t.id}
                    data-state={selected.has(t.id) ? "selected" : undefined}
                  >
                    <TableCell>
                      <Checkbox
                        checked={selected.has(t.id)}
                        onCheckedChange={() => toggle(t.id)}
                        disabled={t.status !== "open"}
                        aria-label="Select task"
                      />
                    </TableCell>
                    <TableCell>
                      <Link
                        to={`/tasks/${t.id}`}
                        className="font-medium text-primary hover:underline"
                      >
                        {t.subject || "(no subject)"}
                      </Link>
                      <div className="font-mono text-xs text-muted-foreground">
                        {shortId(t.id)}
                        {t.external_ref && (
                          <span className="ml-2 rounded bg-muted px-1">
                            ref: {t.external_ref}
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={t.status === "open" ? "success" : "secondary"}
                      >
                        {t.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <ParticipantBadge
                        participant={byId.get(t.initiator_id)}
                        fallbackId={t.initiator_id}
                      />
                    </TableCell>
                    <TableCell className="text-center text-muted-foreground">
                      {t.participants.length}
                    </TableCell>
                    <TableCell className="text-center">
                      {pending === 0 ? (
                        <span className="text-muted-foreground">0</span>
                      ) : (
                        <Badge variant="warning">{pending}</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatRelative(t.last_activity_at)}
                    </TableCell>
                    <TableCell>
                      {t.status === "open" && (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon">
                              <MoreHorizontal className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem
                              className="text-destructive"
                              onClick={() => closeMutation.mutate([t.id])}
                            >
                              Close task
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}
