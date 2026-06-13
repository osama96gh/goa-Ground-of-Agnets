import type { Participant, PendingPair } from "@/lib/types";
import { shortId } from "@/lib/format";
import { ParticipantBadge } from "./ParticipantBadge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/EmptyState";
import { CheckCircle2 } from "lucide-react";

interface Props {
  pending: PendingPair[];
  participants: Map<string, Participant>;
}

export function PendingPairList({ pending, participants }: Props) {
  if (pending.length === 0) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="No pending questions"
        description="Every question on this task has been answered or cancelled."
      />
    );
  }
  return (
    <div className="rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Question id</TableHead>
            <TableHead>Awaiting reply from</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pending.map(([qid, target], idx) => (
            <TableRow key={`${qid}-${target}-${idx}`}>
              <TableCell className="font-mono text-xs">
                {shortId(qid)}…
              </TableCell>
              <TableCell>
                <ParticipantBadge
                  participant={participants.get(target)}
                  fallbackId={target}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
