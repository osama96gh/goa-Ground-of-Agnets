import type { Participant } from "../lib/types";

interface Props {
  participant: Participant | undefined;
  fallbackId?: string | null;
}

export function ParticipantBadge({ participant, fallbackId }: Props) {
  if (!participant) {
    return (
      <span className="font-mono text-xs text-slate-500">
        {fallbackId ? short(fallbackId) : "—"}
      </span>
    );
  }
  const dotColor =
    participant.type === "service" ? "bg-emerald-500" : "bg-sky-500";
  return (
    <span
      title={`${participant.name} · ${participant.type}\n${participant.description}\nCapabilities: ${participant.capabilities.join(", ") || "(none)"}`}
      className="inline-flex items-center gap-1.5 rounded border border-slate-200 bg-white px-1.5 py-0.5 text-xs"
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotColor}`} />
      <span className="font-medium">{participant.name}</span>
      <span className="text-slate-400">{short(participant.id)}</span>
    </span>
  );
}

function short(id: string): string {
  return id.slice(0, 8);
}
