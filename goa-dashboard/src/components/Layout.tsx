import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Brain,
  Command as CommandIcon,
  LayoutDashboard,
  ListTodo,
  LogOut,
  Menu,
  Radio,
  Search,
  Users,
} from "lucide-react";
import { streamFirehose } from "@/api/stream";
import { clearAdminToken } from "@/lib/storage";
import { cn } from "@/lib/utils";
import {
  setFirehoseStale,
  setFirehoseState,
  useFirehoseStatus,
} from "@/lib/firehose-status";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ConnectionStatus } from "@/components/ConnectionStatus";
import { ThemeToggle } from "@/components/ThemeToggle";
import { ErrorBoundary } from "@/components/ErrorBoundary";

interface Props {
  onSignOut: () => void;
  onOpenSearch: () => void;
}

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/timeline", label: "Timeline", icon: Radio },
  { to: "/tasks", label: "Tasks", icon: ListTodo },
  { to: "/participants", label: "Participants", icon: Users },
  { to: "/memory", label: "Memory", icon: Brain },
];

// Stats invalidation is throttled — invalidating on every firehose frame is
// wasteful. Only certain event types meaningfully change aggregate metrics.
const STATS_RELEVANT = new Set([
  "question",
  "answer",
  "cancel_question",
  "cancel_all_questions",
  "child_task_created",
  "parent_closed",
]);

export function Layout({ onSignOut, onOpenSearch }: Props) {
  const qc = useQueryClient();
  const handleRef = useRef<{ close: () => void } | null>(null);
  const lastStatsInvalidation = useRef(0);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const location = useLocation();

  // Close the mobile drawer whenever the route changes.
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  // Start the firehose once at the layout level. Pages subscribe to the
  // resulting React Query cache rather than opening their own SSE — this
  // keeps the whole UI consistent and avoids fan-out reconnection storms.
  useEffect(() => {
    if (handleRef.current) return;
    handleRef.current = streamFirehose({
      onOpen: () => setFirehoseState("open"),
      onReconnecting: () => setFirehoseState("reconnecting"),
      onEvent: (frame) => {
        // Push the event onto the timeline cache. Dedupe by event id so
        // double-fired effects in React StrictMode and reconnection replays
        // don't render the same event twice.
        qc.setQueryData<TimelineEntry[]>(["timeline"], (prev) => {
          const existing = prev ?? [];
          if (existing.some((e) => e.frame.event.id === frame.event.id)) {
            return existing;
          }
          const next: TimelineEntry[] = [
            { frame, receivedAt: Date.now() },
            ...existing,
          ];
          return next.slice(0, 500);
        });
        // Invalidate caches that might be stale.
        qc.invalidateQueries({ queryKey: ["admin", "tasks"] });
        qc.invalidateQueries({ queryKey: ["admin", "task", frame.task_id] });
        // Throttle stats invalidation (~5s) and only for relevant event types.
        if (STATS_RELEVANT.has(frame.event.event_type)) {
          const now = Date.now();
          if (now - lastStatsInvalidation.current > 5_000) {
            lastStatsInvalidation.current = now;
            qc.invalidateQueries({ queryKey: ["admin", "stats"] });
          }
        }
      },
      onGap: () => {
        // We missed events — drop everything and reload from REST.
        setFirehoseStale(true);
        qc.invalidateQueries({ queryKey: ["timeline"] });
        qc.invalidateQueries({ queryKey: ["admin"] });
      },
      onError: (err) => {
        // Logged but non-fatal; fetchEventSource will reconnect on its own.
        console.warn("[firehose]", err);
      },
    });
    return () => {
      handleRef.current?.close();
      handleRef.current = null;
      setFirehoseState("closed");
    };
  }, [qc]);

  const handleSignOut = () => {
    clearAdminToken();
    onSignOut();
  };

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      {/* Desktop sidebar — hidden below md, where it becomes a drawer. */}
      <aside className="hidden w-60 flex-col border-r bg-card md:flex">
        <SidebarBrand />
        <SidebarNav />
        <SidebarFooter onSignOut={handleSignOut} />
      </aside>

      {/* Mobile nav drawer */}
      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Navigation</SheetTitle>
          </SheetHeader>
          <div className="flex h-full flex-col">
            <SidebarBrand />
            <SidebarNav />
            <SidebarFooter onSignOut={handleSignOut} />
          </div>
        </SheetContent>
      </Sheet>

      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center gap-2 border-b bg-card/60 px-3 backdrop-blur sm:px-6">
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0 md:hidden"
            onClick={() => setMobileNavOpen(true)}
            aria-label="Open navigation"
          >
            <Menu className="h-5 w-5" />
          </Button>
          <button
            onClick={onOpenSearch}
            className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border bg-background px-3 text-sm text-muted-foreground transition-colors hover:bg-accent sm:max-w-md"
          >
            <Search className="h-4 w-4 shrink-0" />
            <span className="truncate">
              <span className="hidden sm:inline">
                Search tasks &amp; participants…
              </span>
              <span className="sm:hidden">Search…</span>
            </span>
            <kbd className="ml-auto hidden items-center gap-0.5 rounded border bg-muted px-1.5 py-0.5 text-[10px] font-medium sm:flex">
              <CommandIcon className="h-3 w-3" />K
            </kbd>
          </button>
          <div className="ml-auto flex shrink-0 items-center gap-1 sm:gap-2">
            <ConnectionStatus />
            <ThemeToggle />
          </div>
        </header>

        <StaleBanner />

        <main className="flex-1 overflow-auto">
          <div className="mx-auto max-w-7xl px-4 py-4 sm:px-6 sm:py-6">
            <ErrorBoundary>
              <Outlet />
            </ErrorBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}

function SidebarBrand() {
  return (
    <div className="flex h-14 items-center gap-2 border-b px-5">
      <Link to="/" className="flex items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          G
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">Goa</div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Admin console
          </div>
        </div>
      </Link>
    </div>
  );
}

function SidebarNav() {
  return (
    <nav className="flex-1 space-y-0.5 p-3">
      {NAV.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              isActive
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )
          }
        >
          <item.icon className="h-4 w-4" />
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

function SidebarFooter({ onSignOut }: { onSignOut: () => void }) {
  return (
    <div className="p-3">
      <Separator className="mb-3" />
      <Button
        variant="ghost"
        onClick={onSignOut}
        className="w-full justify-start text-muted-foreground"
      >
        <LogOut className="h-4 w-4" />
        Sign out
      </Button>
    </div>
  );
}

function StaleBanner() {
  const { stale } = useFirehoseStatus();
  if (!stale) return null;
  return (
    <div className="flex items-center gap-2 border-b border-warning/30 bg-warning/10 px-6 py-2 text-sm text-warning">
      <AlertTriangle className="h-4 w-4" />
      <span>
        Missed some live events — data was reloaded and may briefly lag.
      </span>
      <button
        onClick={() => setFirehoseStale(false)}
        className="ml-auto text-xs font-medium underline-offset-2 hover:underline"
      >
        Dismiss
      </button>
    </div>
  );
}

// Exported so pages and the timeline route share the cached shape.
export interface TimelineEntry {
  frame: import("@/lib/types").StreamEventFrame;
  receivedAt: number;
}
