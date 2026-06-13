import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { GoaError, request } from "@/api/client";
import { setAdminToken } from "@/lib/storage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

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
      // Probe a cheap admin endpoint to confirm the token before persisting.
      await request("/admin/participants", { authToken: token });
      setAdminToken(token);
      onUnlocked();
    } catch (err) {
      if (err instanceof GoaError) {
        setError(
          err.status === 401 ? "Invalid token" : `Hub error: ${err.message}`,
        );
      } else {
        setError("Hub unreachable");
      }
      setIsProbing(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <ShieldCheck className="h-5 w-5" />
            </div>
            <CardTitle className="text-xl">Goa Admin Console</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Paste the admin token (the value of <code>GOA_ADMIN_TOKEN</code> on
              the running hub). It is stored locally in your browser.
            </p>
            <div className="space-y-1.5">
              <Label htmlFor="admin-token">Admin token</Label>
              <Input
                id="admin-token"
                type="password"
                autoFocus
                value={value}
                onChange={(e) => setValue(e.target.value)}
                disabled={isProbing}
                className="font-mono"
                placeholder="GOA_ADMIN_TOKEN"
              />
            </div>
            {error && (
              <div
                role="alert"
                className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {error}
              </div>
            )}
            <Button
              type="submit"
              disabled={isProbing || !value.trim()}
              className="w-full"
            >
              {isProbing ? "Checking…" : "Unlock"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
