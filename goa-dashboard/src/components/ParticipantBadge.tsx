import { Bot, Server } from "lucide-react";
import type { Participant } from "@/lib/types";
import { shortId } from "@/lib/format";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface Props {
  participant: Participant | undefined;
  fallbackId?: string | null;
}

export function ParticipantBadge({ participant, fallbackId }: Props) {
  if (!participant) {
    return (
      <span className="font-mono text-xs text-muted-foreground">
        {fallbackId ? shortId(fallbackId) : "—"}
      </span>
    );
  }
  const isService = participant.type === "service";
  const Icon = isService ? Server : Bot;
  const dot = isService ? "bg-emerald-500" : "bg-sky-500";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1.5 rounded-md border bg-card px-1.5 py-0.5 text-xs">
          <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          <Icon className="h-3 w-3 text-muted-foreground" />
          <span className="font-medium">{participant.name}</span>
          <span className="text-muted-foreground">{shortId(participant.id)}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        <div className="font-medium">
          {participant.name} · {participant.type}
        </div>
        {participant.description && (
          <div className="text-muted-foreground">{participant.description}</div>
        )}
        <div className="mt-1 text-muted-foreground">
          Capabilities: {participant.capabilities.join(", ") || "(none)"}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
