import { Link } from "react-router-dom";
import type { TaskListItem } from "@/lib/types";
import { shortId } from "@/lib/format";
import { Badge } from "@/components/ui/badge";

interface Props {
  children: TaskListItem[];
  childrenByParent: Map<string, TaskListItem[]>;
  depth?: number;
}

// Pending lives alongside the task on list-endpoint composites, not inside it.
export function SubTaskTree({ children, childrenByParent, depth = 0 }: Props) {
  if (children.length === 0) {
    return null;
  }
  return (
    <ul className={depth === 0 ? "" : "ml-4 border-l pl-3"}>
      {children.map((item) => {
        const child = item.task;
        const grandkids = childrenByParent.get(child.id) ?? [];
        return (
          <li key={child.id} className="py-1">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                to={`/tasks/${child.id}`}
                className="text-sm font-medium text-primary hover:underline"
              >
                {child.subject || "(no subject)"}
              </Link>
              <span className="font-mono text-xs text-muted-foreground">
                {shortId(child.id)}
              </span>
              {item.pending_questions.length > 0 && (
                <Badge variant="warning">
                  {item.pending_questions.length} pending
                </Badge>
              )}
              <span className="text-xs text-muted-foreground">
                {child.participants.length} participants
              </span>
            </div>
            {grandkids.length > 0 && (
              <SubTaskTree
                children={grandkids}
                childrenByParent={childrenByParent}
                depth={depth + 1}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}
