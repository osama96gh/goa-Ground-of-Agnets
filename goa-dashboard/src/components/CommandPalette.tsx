import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  LayoutDashboard,
  ListTodo,
  Radio,
  Users,
  Brain,
  Hash,
  User,
} from "lucide-react";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { listAdminParticipants, listAdminTasks } from "@/api/admin";

const NAV_ITEMS = [
  { label: "Overview", to: "/", icon: LayoutDashboard },
  { label: "Timeline", to: "/timeline", icon: Radio },
  { label: "Tasks", to: "/tasks", icon: ListTodo },
  { label: "Participants", to: "/participants", icon: Users },
  { label: "Memory", to: "/memory", icon: Brain },
];

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CommandPalette({ open, onOpenChange }: Props) {
  const [search, setSearch] = useState("");
  const navigate = useNavigate();

  // Cmd/Ctrl+K toggles the palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  const trimmed = search.trim();
  const enabled = open && trimmed.length >= 2;

  const { data: tasks = [] } = useQuery({
    queryKey: ["admin", "tasks", "search", trimmed],
    queryFn: () => listAdminTasks({ q: trimmed, limit: 6 }),
    enabled,
  });

  const { data: participants = [] } = useQuery({
    queryKey: ["admin", "participants", "search", trimmed],
    queryFn: () => listAdminParticipants({ q: trimmed }),
    enabled,
  });

  const go = (to: string) => {
    onOpenChange(false);
    setSearch("");
    navigate(to);
  };

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput
        placeholder="Search tasks, participants, or jump to a page…"
        value={search}
        onValueChange={setSearch}
      />
      <CommandList>
        <CommandEmpty>
          {enabled ? "No matches found." : "Type at least 2 characters to search."}
        </CommandEmpty>
        <CommandGroup heading="Navigation">
          {NAV_ITEMS.map((item) => (
            <CommandItem
              key={item.to}
              value={`nav ${item.label}`}
              onSelect={() => go(item.to)}
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </CommandItem>
          ))}
        </CommandGroup>
        {tasks.length > 0 && (
          <CommandGroup heading="Tasks">
            {tasks.map(({ task }) => (
              <CommandItem
                key={task.id}
                value={`task ${task.subject} ${task.id}`}
                onSelect={() => go(`/tasks/${task.id}`)}
              >
                <Hash className="h-4 w-4" />
                <span className="truncate">
                  {task.subject || "(untitled task)"}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {participants.length > 0 && (
          <CommandGroup heading="Participants">
            {participants.slice(0, 6).map((p) => (
              <CommandItem
                key={p.id}
                value={`participant ${p.name} ${p.id}`}
                onSelect={() => go(`/participants?focus=${p.id}`)}
              >
                <User className="h-4 w-4" />
                <span className="truncate">{p.name}</span>
                <span className="ml-auto text-xs text-muted-foreground">
                  {p.type}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  );
}
