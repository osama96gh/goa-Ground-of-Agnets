import { lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  CircleDot,
  HelpCircle,
  ListTodo,
  Server,
  Users,
} from "lucide-react";
import { getAdminStats } from "@/api/admin";
import type { AdminStats } from "@/lib/types";
import { formatRelative } from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";

const OverviewCharts = lazy(() =>
  import("@/components/OverviewCharts").then((m) => ({
    default: ({ stats }: { stats: AdminStats }) => (
      <>
        <ChartCard title="Event volume (14d)">
          <m.EventVolumeChart data={stats.event_volume} />
        </ChartCard>
        <ChartCard title="Tasks by status">
          <m.TasksByStatusChart data={stats.tasks_by_status} />
        </ChartCard>
      </>
    ),
  })),
);

export function OverviewPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin", "stats"],
    queryFn: () => getAdminStats({ window: "14d", recent_limit: 8 }),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="text-sm text-muted-foreground">
          Live health and activity across the coordination hub.
        </p>
      </div>

      {isError ? (
        <EmptyState
          title="Couldn't load metrics"
          description="The /admin/stats endpoint failed. Check the hub is running."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            {isLoading || !data ? (
              Array.from({ length: 8 }).map((_, i) => (
                <Skeleton key={i} className="h-24" />
              ))
            ) : (
              <>
                <Metric
                  label="Total tasks"
                  value={data.totals.tasks}
                  icon={ListTodo}
                  sub={`${data.totals.tasks_open} open · ${data.totals.tasks_closed} closed`}
                />
                <Metric
                  label="Open tasks"
                  value={data.totals.tasks_open}
                  icon={CircleDot}
                  accent="text-primary"
                />
                <Metric
                  label="Pending questions"
                  value={data.totals.pending_questions}
                  icon={HelpCircle}
                  accent={
                    data.totals.pending_questions > 0
                      ? "text-warning"
                      : undefined
                  }
                />
                <Metric
                  label="Events today"
                  value={data.events_today}
                  icon={Activity}
                  sub={`${data.totals.events_total} all-time`}
                />
                <Metric
                  label="Participants"
                  value={data.totals.participants}
                  icon={Users}
                />
                <Metric
                  label="Agents"
                  value={data.totals.participants_agent}
                  icon={Bot}
                />
                <Metric
                  label="Services"
                  value={data.totals.participants_service}
                  icon={Server}
                />
                <Metric
                  label="Closed tasks"
                  value={data.totals.tasks_closed}
                  icon={ListTodo}
                />
              </>
            )}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {isLoading || !data ? (
              <>
                <Skeleton className="h-72" />
                <Skeleton className="h-72" />
              </>
            ) : (
              <Suspense
                fallback={
                  <>
                    <Skeleton className="h-72" />
                    <Skeleton className="h-72" />
                  </>
                }
              >
                <OverviewCharts stats={data} />
              </Suspense>
            )}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <RecentActivity data={data} loading={isLoading} />
            <PendingBacklog data={data} loading={isLoading} />
          </div>
        </>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  icon: Icon,
  sub,
  accent,
}: {
  label: string;
  value: number;
  icon: typeof Activity;
  sub?: string;
  accent?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </span>
          <Icon className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className={`mt-2 text-2xl font-semibold ${accent ?? ""}`}>
          {value.toLocaleString()}
        </div>
        {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function ChartCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function RecentActivity({
  data,
  loading,
}: {
  data?: AdminStats;
  loading: boolean;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Recent activity</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        {loading || !data ? (
          Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))
        ) : data.recent_activity.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No recent activity.
          </p>
        ) : (
          data.recent_activity.map((t) => (
            <Link
              key={t.task_id}
              to={`/tasks/${t.task_id}`}
              className="flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent"
            >
              <Badge
                variant={t.status === "open" ? "success" : "secondary"}
                className="shrink-0"
              >
                {t.status}
              </Badge>
              <span className="flex-1 truncate text-sm">
                {t.subject || "(untitled)"}
              </span>
              {t.pending_count > 0 && (
                <Badge variant="warning" className="shrink-0">
                  {t.pending_count} pending
                </Badge>
              )}
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatRelative(t.last_activity_at)}
              </span>
            </Link>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function PendingBacklog({
  data,
  loading,
}: {
  data?: AdminStats;
  loading: boolean;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Pending question backlog</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        {loading || !data ? (
          Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))
        ) : data.pending_backlog.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No open questions — all caught up.
          </p>
        ) : (
          data.pending_backlog.map((t) => (
            <Link
              key={t.task_id}
              to={`/tasks/${t.task_id}`}
              className="flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent"
            >
              <Badge variant="warning" className="shrink-0">
                {t.pending_count}
              </Badge>
              <span className="flex-1 truncate text-sm">
                {t.subject || "(untitled)"}
              </span>
              {t.oldest_pending_at && (
                <span className="shrink-0 text-xs text-muted-foreground">
                  oldest {formatRelative(t.oldest_pending_at)}
                </span>
              )}
            </Link>
          ))
        )}
      </CardContent>
    </Card>
  );
}
