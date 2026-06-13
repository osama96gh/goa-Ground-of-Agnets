import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { App } from "./App";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { ThemeProvider } from "@/lib/theme";
import { friendlyError, isSuppressedError } from "@/lib/errors";
import "./index.css";

// Global error surfacing: any failed query/mutation toasts a friendly message
// derived from GoaError.code. 401s are suppressed here — App.tsx bounces those
// to the token gate instead of double-reporting them.
const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (err) => {
      if (isSuppressedError(err)) return;
      toast.error(friendlyError(err));
    },
  }),
  mutationCache: new MutationCache({
    onError: (err) => {
      if (isSuppressedError(err)) return;
      toast.error(friendlyError(err));
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={200}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
          <Toaster position="bottom-right" richColors closeButton />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
