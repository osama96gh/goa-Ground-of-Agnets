import { Loader2, Wifi, WifiOff } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useFirehoseStatus } from "@/lib/firehose-status";
import { cn } from "@/lib/utils";

const META = {
  open: { label: "Live", dot: "bg-success", icon: Wifi },
  connecting: { label: "Connecting…", dot: "bg-warning", icon: Loader2 },
  reconnecting: { label: "Reconnecting…", dot: "bg-warning", icon: Loader2 },
  closed: { label: "Disconnected", dot: "bg-destructive", icon: WifiOff },
} as const;

export function ConnectionStatus() {
  const { state } = useFirehoseStatus();
  const meta = META[state];
  const Icon = meta.icon;
  const spinning = state === "connecting" || state === "reconnecting";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex items-center gap-1.5 rounded-full border bg-card px-2.5 py-1 text-xs font-medium text-muted-foreground">
          <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
          <Icon className={cn("h-3 w-3", spinning && "animate-spin")} />
          <span className="hidden sm:inline">{meta.label}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent>Admin event firehose: {meta.label}</TooltipContent>
    </Tooltip>
  );
}
