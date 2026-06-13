import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bot, Brain, Copy, Plus, Search, Server, Trash2 } from "lucide-react";
import {
  deleteAdminParticipant,
  listAdminParticipants,
} from "@/api/admin";
import { ParticipantFormModal } from "@/components/ParticipantFormModal";
import { EmptyState } from "@/components/EmptyState";
import type { Participant } from "@/lib/types";
import { shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
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
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function ParticipantsPage() {
  const qc = useQueryClient();
  const [searchParams] = useSearchParams();
  const focusId = searchParams.get("focus");

  const [searchInput, setSearchInput] = useState("");
  const [q, setQ] = useState("");
  const [type, setType] = useState<"all" | "agent" | "service">("all");
  const [capability, setCapability] = useState("");

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Participant | null>(null);
  const [apiKeyFlash, setApiKeyFlash] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Participant[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    const id = setTimeout(() => setQ(searchInput.trim()), 300);
    return () => clearTimeout(id);
  }, [searchInput]);

  const { data: participants, isLoading } = useQuery({
    queryKey: ["admin", "participants", q, type, capability],
    queryFn: () =>
      listAdminParticipants({
        q: q || undefined,
        type: type === "all" ? undefined : type,
        capability: capability
          ? capability.split(/\s+/).filter(Boolean)
          : undefined,
      }),
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["admin", "participants"] });

  const deleteMutation = useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map(deleteAdminParticipant)),
    onSuccess: (_r, ids) => {
      toast.success(`Deleted ${ids.length} participant${ids.length === 1 ? "" : "s"}.`);
      setSelected(new Set());
      setConfirmDelete(null);
      invalidate();
    },
  });

  const list = participants ?? [];
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Participants</h1>
          <p className="text-sm text-muted-foreground">
            Registry of agents and services. Capability filter is AND-ed.
          </p>
        </div>
        <div className="flex gap-2">
          {selected.size > 0 && (
            <Button
              variant="destructive"
              onClick={() =>
                setConfirmDelete(list.filter((p) => selected.has(p.id)))
              }
            >
              <Trash2 className="h-4 w-4" />
              Delete {selected.size}
            </Button>
          )}
          <Button
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
            <Plus className="h-4 w-4" />
            Add participant
          </Button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search name or description…"
            className="pl-8"
          />
        </div>
        <Input
          value={capability}
          onChange={(e) => setCapability(e.target.value)}
          placeholder="capabilities (space-separated)"
          className="w-60"
        />
        <Select value={type} onValueChange={(v) => setType(v as typeof type)}>
          <SelectTrigger className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            <SelectItem value="agent">Agents</SelectItem>
            <SelectItem value="service">Services</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon={Bot}
          title="No participants match"
          description="Adjust the filters, or add a participant to get started."
        />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <Checkbox
                    checked={
                      list.length > 0 && list.every((p) => selected.has(p.id))
                    }
                    onCheckedChange={(c) =>
                      setSelected(c ? new Set(list.map((p) => p.id)) : new Set())
                    }
                    aria-label="Select all"
                  />
                </TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Capabilities</TableHead>
                <TableHead>ID</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.map((p) => (
                <TableRow
                  key={p.id}
                  data-state={selected.has(p.id) ? "selected" : undefined}
                  className={cn(focusId === p.id && "ring-2 ring-inset ring-primary")}
                >
                  <TableCell>
                    <Checkbox
                      checked={selected.has(p.id)}
                      onCheckedChange={() => toggle(p.id)}
                      aria-label="Select participant"
                    />
                  </TableCell>
                  <TableCell>
                    <div className="font-medium">{p.name}</div>
                    {p.description && (
                      <div className="text-xs text-muted-foreground">
                        {p.description}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="gap-1">
                      {p.type === "service" ? (
                        <Server className="h-3 w-3" />
                      ) : (
                        <Bot className="h-3 w-3" />
                      )}
                      {p.type}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {p.capabilities.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {p.capabilities.map((c) => (
                          <Badge key={c} variant="secondary">
                            {c}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {shortId(p.id)}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center justify-end gap-1">
                      <Button variant="ghost" size="sm" asChild>
                        <Link to={`/memory?participant=${p.id}`}>
                          <Brain className="h-4 w-4" />
                          Memory
                        </Link>
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setEditing(p);
                          setFormOpen(true);
                        }}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => setConfirmDelete([p])}
                        aria-label="Delete participant"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Add / edit modal — keyed so state resets between opens. */}
      {formOpen &&
        (editing ? (
          <ParticipantFormModal
            key={editing.id}
            mode="edit"
            initialValues={editing}
            open={formOpen}
            onOpenChange={setFormOpen}
            onSuccess={() => {
              setFormOpen(false);
              invalidate();
              toast.success("Participant updated.");
            }}
          />
        ) : (
          <ParticipantFormModal
            key="add"
            mode="add"
            open={formOpen}
            onOpenChange={setFormOpen}
            onSuccess={({ api_key }) => {
              setFormOpen(false);
              invalidate();
              if (api_key) setApiKeyFlash(api_key);
            }}
          />
        ))}

      {/* API key reveal-once dialog */}
      <Dialog
        open={apiKeyFlash !== null}
        onOpenChange={(o) => !o && setApiKeyFlash(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>API key created</DialogTitle>
            <DialogDescription>
              Copy this now — it is shown only once and cannot be retrieved later.
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2 rounded-md border bg-muted/50 p-3">
            <code className="flex-1 break-all font-mono text-xs">
              {apiKeyFlash}
            </code>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                if (apiKeyFlash) {
                  navigator.clipboard.writeText(apiKeyFlash);
                  toast.success("Copied to clipboard.");
                }
              }}
            >
              <Copy className="h-3.5 w-3.5" />
              Copy
            </Button>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button>Done</Button>
            </DialogClose>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog
        open={confirmDelete !== null}
        onOpenChange={(o) => !o && setConfirmDelete(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Delete {confirmDelete?.length === 1 ? "participant" : "participants"}?
            </DialogTitle>
            <DialogDescription>
              This permanently removes{" "}
              {confirmDelete?.length === 1
                ? confirmDelete[0].name
                : `${confirmDelete?.length} participants`}{" "}
              and purges all of their agent-private memory. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() =>
                confirmDelete &&
                deleteMutation.mutate(confirmDelete.map((p) => p.id))
              }
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
