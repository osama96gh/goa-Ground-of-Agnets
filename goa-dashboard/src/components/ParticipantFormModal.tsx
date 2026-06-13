import { useState } from "react";
import type { Participant, ParticipantType } from "@/lib/types";
import { createAdminParticipant, updateAdminParticipant } from "@/api/admin";
import { friendlyError } from "@/lib/errors";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (result: { participant: Participant; api_key?: string }) => void;
} & (
  | { mode: "add"; initialValues?: undefined }
  | { mode: "edit"; initialValues: Participant }
);

export function ParticipantFormModal(props: Props) {
  const { mode, open, onOpenChange, onSuccess } = props;
  const [name, setName] = useState(props.initialValues?.name ?? "");
  const [type, setType] = useState<ParticipantType>(
    props.initialValues?.type ?? "agent",
  );
  const [description, setDescription] = useState(
    props.initialValues?.description ?? "",
  );
  const [capabilities, setCapabilities] = useState(
    (props.initialValues?.capabilities ?? []).join(" "),
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const caps = capabilities.split(/\s+/).filter(Boolean);
    try {
      if (props.mode === "add") {
        const res = await createAdminParticipant({
          type,
          name,
          description,
          capabilities: caps,
        });
        onSuccess({ participant: res.participant, api_key: res.api_key });
      } else {
        const updated = await updateAdminParticipant(props.initialValues.id, {
          name,
          description,
          capabilities: caps,
        });
        onSuccess({ participant: updated });
      }
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {mode === "add" ? "Add participant" : "Edit participant"}
          </DialogTitle>
          <DialogDescription>
            {mode === "add"
              ? "Register a new agent or service. The API key is shown once on creation."
              : "Update this participant's details. Type is immutable."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="p-name">Name</Label>
            <Input
              id="p-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              minLength={1}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Type</Label>
            <Select
              value={type}
              onValueChange={(v) => setType(v as ParticipantType)}
              disabled={mode === "edit"}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="agent">agent</SelectItem>
                <SelectItem value="service">service</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="p-desc">Description</Label>
            <textarea
              id="p-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="flex w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="p-caps">
              Capabilities{" "}
              <span className="font-normal text-muted-foreground">
                (space-separated)
              </span>
            </Label>
            <Input
              id="p-caps"
              value={capabilities}
              onChange={(e) => setCapabilities(e.target.value)}
              placeholder="payments support"
            />
          </div>
          {error && (
            <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? "Saving…" : mode === "add" ? "Create" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
