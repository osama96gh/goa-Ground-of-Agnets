import { useState } from "react";
import type { Participant, ParticipantType } from "../lib/types";
import {
  createAdminParticipant,
  updateAdminParticipant,
} from "../api/admin";

type Props = {
  onClose: () => void;
  onSuccess: (result: { participant: Participant; api_key?: string }) => void;
} & (
  | { mode: "add"; initialValues?: undefined }
  | { mode: "edit"; initialValues: Participant }
);

export function ParticipantFormModal(props: Props) {
  const { mode, onClose, onSuccess } = props;
  const [name, setName] = useState(props.initialValues?.name ?? "");
  const [type, setType] = useState<ParticipantType>(
    props.initialValues?.type ?? "agent",
  );
  const [description, setDescription] = useState(
    props.initialValues?.description ?? "",
  );
  const [capabilities, setCapabilities] = useState(
    (props.initialValues?.capabilities ?? []).join(" "),
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const caps = capabilities.split(/\s+/).filter(Boolean);
    try {
      if (props.mode === "add") {
        const res = await createAdminParticipant({
          type,
          name,
          description,
          capabilities: caps,
        });
        onSuccess({ participant: res.participant, api_key: res.api_key });
      } else {
        // Discriminated union narrows initialValues to Participant here.
        const updated = await updateAdminParticipant(props.initialValues.id, {
          name,
          description,
          capabilities: caps,
        });
        onSuccess({ participant: updated });
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-6 shadow-lg">
        <h2 className="mb-4 text-lg font-semibold">
          {mode === "add" ? "Add participant" : "Edit participant"}
        </h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              minLength={1}
              className="mt-1 block w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Type</span>
            <select
              value={type}
              onChange={(e) => setType(e.target.value as ParticipantType)}
              disabled={mode === "edit"}
              className="mt-1 block w-full rounded border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50 disabled:text-slate-400"
            >
              <option value="agent">agent</option>
              <option value="service">service</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-600">
              Description
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="mt-1 block w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-600">
              Capabilities{" "}
              <span className="font-normal text-slate-400">
                (space-separated)
              </span>
            </span>
            <input
              type="text"
              value={capabilities}
              onChange={(e) => setCapabilities(e.target.value)}
              placeholder="payments support"
              className="mt-1 block w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
            />
          </label>
          {error && (
            <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="rounded bg-slate-800 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {loading ? "Saving…" : mode === "add" ? "Create" : "Save"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
