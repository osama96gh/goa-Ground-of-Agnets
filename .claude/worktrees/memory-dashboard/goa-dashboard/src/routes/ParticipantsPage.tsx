import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listAdminParticipants, deleteAdminParticipant } from "../api/admin";
import { ParticipantFormModal } from "../components/ParticipantFormModal";
import type { Participant } from "../lib/types";

export function ParticipantsPage() {
  const queryClient = useQueryClient();

  const [q, setQ] = useState("");
  const [type, setType] = useState<"" | "agent" | "service">("");
  const [capability, setCapability] = useState("");

  const [formModal, setFormModal] = useState<
    | { mode: "add" }
    | { mode: "edit"; participant: Participant }
    | null
  >(null);
  const [apiKeyFlash, setApiKeyFlash] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const { data: participants, isLoading } = useQuery({
    queryKey: ["admin", "participants", q, type, capability],
    queryFn: () =>
      listAdminParticipants({
        q: q || undefined,
        type: type || undefined,
        capability: capability ? capability.split(/\s+/).filter(Boolean) : undefined,
      }),
  });

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["admin", "participants"] });
  }

  async function handleDelete(id: string) {
    if (pendingDelete !== id) {
      setPendingDelete(id);
      return;
    }
    setDeleting(true);
    try {
      await deleteAdminParticipant(id);
      invalidate();
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  }

  return (
    <div className="space-y-4">
      {apiKeyFlash && (
        <div className="flex items-start gap-3 rounded-md border border-green-300 bg-green-50 px-4 py-3">
          <div className="flex-1">
            <p className="text-sm font-medium text-green-800">
              API key (shown once — copy now)
            </p>
            <p className="mt-1 break-all font-mono text-xs text-green-700">
              {apiKeyFlash}
            </p>
          </div>
          <button
            onClick={() => {
              navigator.clipboard.writeText(apiKeyFlash);
            }}
            className="shrink-0 rounded border border-green-300 bg-white px-2 py-1 text-xs text-green-700 hover:bg-green-100"
          >
            Copy
          </button>
          <button
            onClick={() => setApiKeyFlash(null)}
            className="shrink-0 text-green-500 hover:text-green-700"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Participants</h1>
          <p className="text-sm text-slate-500">
            Registry directory. Capability filter is space-separated and AND-ed.
          </p>
        </div>
        <button
          onClick={() => setFormModal({ mode: "add" })}
          className="rounded bg-slate-800 px-3 py-1.5 text-sm text-white hover:bg-slate-700"
        >
          + Add participant
        </button>
      </div>

      <div className="grid grid-cols-3 gap-3 rounded-md border border-slate-200 bg-white p-3">
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Search</span>
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="name or description"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Capabilities</span>
          <input
            type="text"
            value={capability}
            onChange={(e) => setCapability(e.target.value)}
            placeholder="payments support"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-slate-600">Type</span>
          <select
            value={type}
            onChange={(e) => setType(e.target.value as "" | "agent" | "service")}
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">all</option>
            <option value="agent">agent</option>
            <option value="service">service</option>
          </select>
        </label>
      </div>

      {isLoading && <div className="text-sm text-slate-500">Loading…</div>}
      <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Capabilities</th>
              <th className="px-3 py-2">Description</th>
              <th className="px-3 py-2">ID</th>
              <th className="px-3 py-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(participants ?? []).map((p) => (
              <tr key={p.id} className="border-t border-slate-100">
                <td className="px-3 py-2 font-medium">{p.name}</td>
                <td className="px-3 py-2">{p.type}</td>
                <td className="px-3 py-2">
                  {p.capabilities.length === 0 ? (
                    <span className="text-slate-400">(none)</span>
                  ) : (
                    p.capabilities.map((c) => (
                      <span
                        key={c}
                        className="mr-1 inline-block rounded bg-slate-100 px-1.5 py-0.5 text-xs"
                      >
                        {c}
                      </span>
                    ))
                  )}
                </td>
                <td className="px-3 py-2 text-slate-600">{p.description || "—"}</td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">
                  {p.id.slice(0, 8)}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/memory?participant=${p.id}`}
                      className="text-xs text-slate-500 underline hover:text-slate-800"
                    >
                      Memory
                    </Link>
                    <button
                      onClick={() => setFormModal({ mode: "edit", participant: p })}
                      className="text-xs text-slate-500 underline hover:text-slate-800"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleDelete(p.id)}
                      disabled={deleting && pendingDelete === p.id}
                      className={`text-xs underline ${
                        pendingDelete === p.id
                          ? "text-red-600 hover:text-red-800"
                          : "text-slate-500 hover:text-red-600"
                      }`}
                    >
                      {pendingDelete === p.id
                        ? deleting
                          ? "Deleting…"
                          : "Confirm?"
                        : "Delete"}
                    </button>
                    {pendingDelete === p.id && !deleting && (
                      <button
                        onClick={() => setPendingDelete(null)}
                        className="text-xs text-slate-400 underline hover:text-slate-600"
                      >
                        Cancel
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {participants && participants.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                  No participants match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {formModal &&
        (formModal.mode === "add" ? (
          <ParticipantFormModal
            mode="add"
            onClose={() => setFormModal(null)}
            onSuccess={({ api_key }) => {
              setFormModal(null);
              invalidate();
              if (api_key) {
                setApiKeyFlash(api_key);
              }
            }}
          />
        ) : (
          <ParticipantFormModal
            mode="edit"
            initialValues={formModal.participant}
            onClose={() => setFormModal(null)}
            onSuccess={() => {
              setFormModal(null);
              invalidate();
            }}
          />
        ))}
    </div>
  );
}
