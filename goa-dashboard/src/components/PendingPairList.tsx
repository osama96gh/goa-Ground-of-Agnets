import type { Participant, PendingPair } from "../lib/types";
import { ParticipantBadge } from "./ParticipantBadge";

interface Props {
  pending: PendingPair[];
  participants: Map<string, Participant>;
}

export function PendingPairList({ pending, participants }: Props) {
  if (pending.length === 0) {
    return (
      <div className="rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-500">
        No pending questions on this task.
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
          <tr>
            <th className="px-3 py-2">Question id</th>
            <th className="px-3 py-2">Awaiting reply from</th>
          </tr>
        </thead>
        <tbody>
          {pending.map(([qid, target], idx) => (
            <tr
              key={`${qid}-${target}-${idx}`}
              className="border-t border-slate-100"
            >
              <td className="px-3 py-2 font-mono text-xs">{qid.slice(0, 8)}…</td>
              <td className="px-3 py-2">
                <ParticipantBadge
                  participant={participants.get(target)}
                  fallbackId={target}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
