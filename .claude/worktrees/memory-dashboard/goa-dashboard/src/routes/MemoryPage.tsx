import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { listAdminParticipants } from "../api/admin";
import { listAdminParticipantMemory } from "../api/memory";

// Read-only inspector for a participant's agent-private memory (§9). The
// owning participant is the only writer; operators read via the admin token.
export function MemoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const participantId = searchParams.get("participant") ?? "";

  const [prefix, setPrefix] = useState("");
  const [tag, setTag] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data: participants } = useQuery({
    queryKey: ["admin", "participants"],
    queryFn: () => listAdminParticipants(),
  });

  const tagList = tag.split(/\s+/).filter(Boolean);
  const {
    data: entries,
    isLoading,
    isError,
    error,
  } = useQuery({
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
    setExpanded(new Set());
  }

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Memory</h1>
        <p className="text-sm text-slate-500">
          Agent-private memory, read-only. Select a participant to inspect its
          entries. Prefix is an exact key-prefix scan; tags are space-separated
          and AND-ed.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-3 rounded-md border border-slate-200 bg-white p-3">
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Participant</span>
          <select
            value={participantId}
            onChange={(e) => selectParticipant(e.target.value)}
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">Select a participant…</option>
            {(participants ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.type})
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Key prefix</span>
          <input
            type="text"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
            placeholder="user:U123:"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Tags</span>
          <input
            type="text"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            placeholder="user prefs"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
      </div>

      {!participantId && (
        <div className="rounded-md border border-slate-200 bg-white px-4 py-6 text-center text-sm text-slate-500">
          Select a participant to view its memory.
        </div>
      )}

      {participantId && isError && (
        <div className="rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error instanceof Error ? error.message : "Failed to load memory."}
        </div>
      )}

      {participantId && isLoading && (
        <div className="text-sm text-slate-500">Loading…</div>
      )}

      {participantId && !isError && (
        <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Key</th>
                <th className="px-3 py-2">Tags</th>
                <th className="px-3 py-2">Value</th>
                <th className="px-3 py-2">Updated</th>
              </tr>
            </thead>
            <tbody>
              {(entries ?? []).map((e) => {
                const isOpen = expanded.has(e.id);
                return (
                  <Fragment key={e.id}>
                    <tr className="border-t border-slate-100 align-top">
                      <td className="px-3 py-2 font-mono text-xs">{e.key}</td>
                      <td className="px-3 py-2">
                        {e.tags.length === 0 ? (
                          <span className="text-slate-400">—</span>
                        ) : (
                          e.tags.map((t) => (
                            <span
                              key={t}
                              className="mr-1 inline-block rounded bg-slate-100 px-1.5 py-0.5 text-xs"
                            >
                              {t}
                            </span>
                          ))
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-start gap-2">
                          <code className="flex-1 break-all font-mono text-xs text-slate-700">
                            {preview(e.value)}
                          </code>
                          <button
                            onClick={() => toggle(e.id)}
                            className="shrink-0 text-xs text-blue-600 hover:underline"
                          >
                            {isOpen ? "Hide" : "View"}
                          </button>
                        </div>
                      </td>
                      <td className="px-3 py-2 text-slate-500">
                        {new Date(e.updated_at).toLocaleString()}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="border-t border-slate-100 bg-slate-50">
                        <td colSpan={4} className="px-3 py-2">
                          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-white p-3 font-mono text-xs text-slate-800">{JSON.stringify(e.value, null, 2)}</pre>
                          <div className="mt-1 text-xs text-slate-400">
                            id {e.id.slice(0, 8)} · created{" "}
                            {new Date(e.created_at).toLocaleString()}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {entries && entries.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-slate-500">
                    No memory entries match.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// Compact one-line JSON preview for the table cell.
function preview(v: unknown): string {
  const s = JSON.stringify(v);
  if (s === undefined) return "—";
  return s.length > 80 ? `${s.slice(0, 80)}…` : s;
}
