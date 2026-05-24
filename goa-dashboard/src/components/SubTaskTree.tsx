import { Link } from "react-router-dom";
import type { TaskListItem } from "../lib/types";

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
    <ul className={depth === 0 ? "" : "ml-4 border-l border-slate-200 pl-3"}>
      {children.map((item) => {
        const child = item.task;
        const grandkids = childrenByParent.get(child.id) ?? [];
        return (
          <li key={child.id} className="py-1">
            <Link
              to={`/tasks/${child.id}`}
              className="text-sm text-blue-600 hover:underline"
            >
              {child.subject || "(no subject)"}{" "}
              <span className="font-mono text-xs text-slate-400">
                {child.id.slice(0, 8)}
              </span>
            </Link>
            <span className="ml-2 text-xs text-slate-500">
              {item.pending_questions.length} pending · {child.participants.length} participants
            </span>
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
