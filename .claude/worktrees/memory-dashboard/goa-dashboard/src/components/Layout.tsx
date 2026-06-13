import { useEffect, useRef } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { streamFirehose } from "../api/stream";
import { clearAdminToken } from "../lib/storage";

interface Props {
  onSignOut: () => void;
}

const NAV = [
  { to: "/", label: "Timeline", end: true },
  { to: "/tasks", label: "Tasks" },
  { to: "/participants", label: "Participants" },
  { to: "/memory", label: "Memory" },
];

export function Layout({ onSignOut }: Props) {
  const qc = useQueryClient();
  // Start the firehose once at the layout level. Pages subscribe to the
  // resulting React Query cache rather than opening their own SSE — this
  // keeps the whole UI consistent and avoids fan-out reconnection storms.
  const handleRef = useRef<{ close: () => void } | null>(null);

  useEffect(() => {
    if (handleRef.current) return;
    handleRef.current = streamFirehose({
      onEvent: (frame) => {
        // Push the event onto the timeline cache. Dedupe by event id so
        // double-fired effects in React StrictMode and reconnection replays
        // don't render the same event twice.
        qc.setQueryData<TimelineEntry[]>(
          ["timeline"],
          (prev) => {
            const existing = prev ?? [];
            if (existing.some((e) => e.frame.event.id === frame.event.id)) {
              return existing;
            }
            const next: TimelineEntry[] = [
              { frame, receivedAt: Date.now() },
              ...existing,
            ];
            return next.slice(0, 500);
          },
        );
        // Invalidate caches that might be stale.
        qc.invalidateQueries({ queryKey: ["admin", "tasks"] });
        qc.invalidateQueries({ queryKey: ["admin", "task", frame.task_id] });
      },
      onGap: () => {
        // We missed events — drop everything and reload from REST.
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
    };
  }, [qc]);

  const handleSignOut = () => {
    clearAdminToken();
    onSignOut();
  };

  return (
    <div className="flex min-h-screen bg-slate-50">
      <aside className="flex w-56 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-4 py-3">
          <Link to="/" className="text-base font-semibold text-slate-900">
            Goa
          </Link>
          <div className="text-xs text-slate-500">v2 dashboard</div>
        </div>
        <nav className="flex-1 px-2 py-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block rounded px-3 py-2 text-sm ${
                  isActive
                    ? "bg-blue-50 font-medium text-blue-700"
                    : "text-slate-700 hover:bg-slate-100"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-200 px-2 py-3">
          <button
            onClick={handleSignOut}
            className="block w-full rounded px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100"
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <div className="mx-auto max-w-6xl px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

// Exported so pages and the timeline route share the cached shape.
export interface TimelineEntry {
  frame: import("../lib/types").StreamEventFrame;
  receivedAt: number;
}
