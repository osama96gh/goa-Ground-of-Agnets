// Recharts-backed charts for the Overview page. Imported lazily so recharts
// (the heaviest dependency) stays out of the initial bundle.

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { AdminStats } from "@/lib/types";

const AXIS = "hsl(var(--muted-foreground))";
const GRID = "hsl(var(--border))";

function tooltipStyle() {
  return {
    backgroundColor: "hsl(var(--popover))",
    border: "1px solid hsl(var(--border))",
    borderRadius: "0.5rem",
    fontSize: "0.75rem",
    color: "hsl(var(--popover-foreground))",
  };
}

export function EventVolumeChart({
  data,
}: {
  data: AdminStats["event_volume"];
}) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id="evgrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
            <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 11, fill: AXIS }}
          tickFormatter={(d: string) => d.slice(5)}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fontSize: 11, fill: AXIS }}
          axisLine={false}
          tickLine={false}
          allowDecimals={false}
          width={32}
        />
        <Tooltip contentStyle={tooltipStyle()} cursor={{ stroke: GRID }} />
        <Area
          type="monotone"
          dataKey="count"
          name="Events"
          stroke="hsl(var(--primary))"
          strokeWidth={2}
          fill="url(#evgrad)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function TasksByStatusChart({
  data,
}: {
  data: AdminStats["tasks_by_status"];
}) {
  const rows = [
    { name: "Open", value: data.open, fill: "hsl(var(--primary))" },
    { name: "Closed", value: data.closed, fill: "hsl(var(--muted-foreground))" },
  ];
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={rows} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
        <XAxis
          dataKey="name"
          tick={{ fontSize: 11, fill: AXIS }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fontSize: 11, fill: AXIS }}
          axisLine={false}
          tickLine={false}
          allowDecimals={false}
          width={32}
        />
        <Tooltip contentStyle={tooltipStyle()} cursor={{ fill: "hsl(var(--accent))" }} />
        <Bar dataKey="value" name="Tasks" radius={[4, 4, 0, 0]}>
          {rows.map((r) => (
            <Cell key={r.name} fill={r.fill} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
