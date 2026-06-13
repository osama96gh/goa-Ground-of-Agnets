import { useState } from "react";
import { GoaError, request } from "../api/client";
import { setAdminToken } from "../lib/storage";

interface Props {
  onUnlocked: () => void;
}

export function AdminTokenGate({ onUnlocked }: Props) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isProbing, setIsProbing] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const token = value.trim();
    if (!token || isProbing) return;
    setError(null);
    setIsProbing(true);
    try {
      // Probe a cheap admin endpoint to confirm the token is accepted before
      // we persist it. Using the typed token directly (not via storage) so a
      // bad token never lands in localStorage.
      await request("/admin/participants", { authToken: token });
      setAdminToken(token);
      onUnlocked();
    } catch (err) {
      if (err instanceof GoaError) {
        setError(err.status === 401 ? "Invalid token" : `Hub error: ${err.message}`);
      } else {
        setError("Hub unreachable");
      }
      setIsProbing(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
      >
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Goa Dashboard</h1>
          <p className="mt-1 text-sm text-slate-500">
            Paste the admin token (the value of <code>GOA_ADMIN_TOKEN</code> on
            the running hub) to view the live event firehose. The token is
            stored locally in your browser.
          </p>
        </div>
        <div>
          <label className="block text-sm font-medium text-slate-700">
            Admin token
          </label>
          <input
            type="password"
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={isProbing}
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:bg-slate-100"
            placeholder="GOA_ADMIN_TOKEN"
          />
        </div>
        {error && (
          <div
            role="alert"
            className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {error}
          </div>
        )}
        <button
          type="submit"
          disabled={isProbing || !value.trim()}
          className="w-full rounded-md bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-blue-300"
        >
          {isProbing ? "Checking…" : "Unlock"}
        </button>
      </form>
    </div>
  );
}
