import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { AdminTokenGate } from "./components/AdminTokenGate";
import { Layout } from "./components/Layout";
import { MemoryPage } from "./routes/MemoryPage";
import { ParticipantsPage } from "./routes/ParticipantsPage";
import { TaskDetailPage } from "./routes/TaskDetailPage";
import { TasksPage } from "./routes/TasksPage";
import { TimelinePage } from "./routes/TimelinePage";
import { GoaError } from "./api/client";
import { getAdminToken, setAdminToken } from "./lib/storage";

// In dev, Vite inlines GOA_ADMIN_TOKEN from the workspace .env so `make demo`
// opens a usable dashboard with no manual paste. Production builds get the
// empty string (see vite.config.ts) and fall through to the prompt.
const DEV_ADMIN_TOKEN: string =
  import.meta.env.DEV ? (import.meta.env.VITE_GOA_ADMIN_TOKEN ?? "") : "";

function readInitialToken(): string | null {
  const stored = getAdminToken();
  if (stored) return stored;
  if (DEV_ADMIN_TOKEN) {
    setAdminToken(DEV_ADMIN_TOKEN);
    return DEV_ADMIN_TOKEN;
  }
  return null;
}

export function App() {
  const [hasToken, setHasToken] = useState<boolean>(() => Boolean(readInitialToken()));
  const queryClient = useQueryClient();

  // If any query throws a 401 (e.g. the hub's GOA_ADMIN_TOKEN was rotated
  // mid-session), client.ts has already cleared storage; bounce back to the
  // gate so the user can re-enter a valid token.
  useEffect(() => {
    if (!hasToken) return;
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      // react-query emits "failed" on each retry attempt and "error" once
      // retries are exhausted. Bounce on the first 401 — no point waiting for
      // 3 retries to confirm the token is bad.
      if (
        event.type === "updated" &&
        (event.action.type === "failed" || event.action.type === "error")
      ) {
        const err = event.action.error;
        if (err instanceof GoaError && err.status === 401) {
          setHasToken(false);
        }
      }
    });
    return unsubscribe;
  }, [hasToken, queryClient]);

  if (!hasToken) {
    return <AdminTokenGate onUnlocked={() => setHasToken(true)} />;
  }

  return (
    <Routes>
      <Route element={<Layout onSignOut={() => setHasToken(false)} />}>
        <Route index element={<TimelinePage />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="tasks/:taskId" element={<TaskDetailPage />} />
        <Route path="participants" element={<ParticipantsPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="*" element={<TimelinePage />} />
      </Route>
    </Routes>
  );
}
