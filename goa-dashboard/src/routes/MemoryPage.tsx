import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Brain } from "lucide-react";
import { listAdminParticipants } from "@/api/admin";
import { listAdminParticipantMemory } from "@/api/memory";
import type { MemoryEntry } from "@/lib/types";
import { formatDateTime, shortId } from "@/lib/format";
import { EmptyState } from "@/components/EmptyState";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export function MemoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const participantId = searchParams.get("participant") ?? "";

  const [prefix, setPrefix] = useState("");
  const [tag, setTag] = useState("");
  const [detail, setDetail] = useState<MemoryEntry | null>(null);

  const { data: participants } = useQuery({
    queryKey: ["admin", "participants"],
    queryFn: () => listAdminParticipants(),
  });

  const tagList = tag.split(/\s+/).filter(Boolean);
  const { data: entries, isLoading } = useQuery({
    queryKey: ["admin", "memory", participantId, prefix, tag],
    queryFn: () =>
      listAdminParticipantMemory(participantId, {
        prefix: prefix || undefined,
        tag: tagList.length ? tagList : undefined,
      }),
    enabled: Boolean(participantId),
  });

  function selectParticipant(id: string) {
    const next = new URLSearchParams(searchParams);
    if (id) next.set("participant", id);
    else next.delete("participant");
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Memory</h1>
        <p className="text-sm text-muted-foreground">
          Agent-private memory, read-only. Prefix is an exact key-prefix scan;
          tags are space-separated and AND-ed.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3">
        <Select
          value={participantId || undefined}
          onValueChange={selectParticipant}
        >
          <SelectTrigger className="w-64">
            <SelectValue placeholder="Select a participant…" />
          </SelectTrigger>
          <SelectContent>
            {(participants ?? []).map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name} ({p.type})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          value={prefix}
          onChange={(e) => setPrefix(e.target.value)}
          placeholder="key prefix, e.g. user:U123:"
          className="w-56"
          disabled={!participantId}
        />
        <Input
          value={tag}
          onChange={(e) => setTag(e.target.value)}
          placeholder="tags (space-separated)"
          className="w-56"
          disabled={!participantId}
        />
      </div>

      {!participantId ? (
        <EmptyState
          icon={Brain}
          title="Select a participant"
          description="Choose a participant above to inspect its agent-private memory."
        />
      ) : isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : (entries ?? []).length === 0 ? (
        <EmptyState title="No memory entries match" />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Key</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>Value</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(entries ?? []).map((e) => (
                <TableRow
                  key={e.id}
                  className="cursor-pointer"
                  onClick={() => setDetail(e)}
                >
                  <TableCell className="font-mono text-xs">{e.key}</TableCell>
                  <TableCell>
                    {e.tags.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {e.tags.map((t) => (
                          <Badge key={t} variant="secondary">
                            {t}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs truncate font-mono text-xs text-muted-foreground">
                    {preview(e.value)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDateTime(e.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Sheet open={detail !== null} onOpenChange={(o) => !o && setDetail(null)}>
        <SheetContent className="w-full sm:max-w-xl">
          {detail && (
            <>
              <SheetHeader>
                <SheetTitle className="break-all font-mono text-sm">
                  {detail.key}
                </SheetTitle>
                <SheetDescription>
                  id {shortId(detail.id)} · created{" "}
                  {formatDateTime(detail.created_at)} · updated{" "}
                  {formatDateTime(detail.updated_at)}
                </SheetDescription>
              </SheetHeader>
              {detail.tags.length > 0 && (
                <div className="mt-4 flex flex-wrap gap-1">
                  {detail.tags.map((t) => (
                    <Badge key={t} variant="secondary">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
              <pre className="mt-4 max-h-[70vh] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/50 p-3 font-mono text-xs">
                {JSON.stringify(detail.value, null, 2)}
              </pre>
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function preview(v: unknown): string {
  const s = JSON.stringify(v);
  if (s === undefined) return "—";
  return s;
}
