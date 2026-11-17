import type { CSSProperties } from "react";
import { CircleDashed } from "lucide-react";

import { humanize } from "@/lib/format";
import { isAssignmentStatus, railState, RAIL_STAGES, STATUS_META } from "@/lib/status";
import type { AssignmentStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The status of one application. An icon and a word, always together, with the
 * icon in the status colour the charts use for the same stage.
 */
export function ApplicationStatusBadge({
  status,
  className,
  size = "default",
}: {
  status: AssignmentStatus | string;
  className?: string;
  size?: "default" | "lg";
}) {
  const meta = isAssignmentStatus(status) ? STATUS_META[status] : null;
  const Icon = meta?.icon ?? CircleDashed;
  const color = meta?.color ?? "var(--status-closed)";
  return (
    <span
      data-slot="status-badge"
      data-status={status}
      className={cn(
        "inline-flex w-fit shrink-0 items-center gap-1.5 rounded-[5px] border font-medium whitespace-nowrap text-foreground",
        size === "lg" ? "h-7 px-2.5 text-sm" : "h-5.5 px-1.5 text-xs",
        className,
      )}
      style={
        {
          backgroundColor: `color-mix(in oklch, ${color} 12%, var(--card))`,
          borderColor: `color-mix(in oklch, ${color} 38%, var(--border))`,
        } as CSSProperties
      }
    >
      <Icon className={size === "lg" ? "size-4" : "size-3.5"} style={{ color }} aria-hidden="true" />
      {meta?.label ?? humanize(status)}
    </span>
  );
}

/**
 * The five-stage pipeline as a rail: Assigned, Viewed, Applied, Interview, Offer.
 * Filled segments are stages the application has reached, each in its own stage
 * colour. An application that closed keeps what it reached in grey and hatches
 * the rest, so "rejected after applying" and "skipped unread" look different.
 */
export function StageRail({
  status,
  viewedAt,
  appliedAt,
  interviewedAt,
  size = "default",
  showLabels = false,
  className,
}: {
  status: AssignmentStatus;
  viewedAt?: string | null;
  appliedAt?: string | null;
  interviewedAt?: string | null;
  size?: "default" | "lg";
  showLabels?: boolean;
  className?: string;
}) {
  const state = railState({ status, viewed_at: viewedAt, applied_at: appliedAt, interviewed_at: interviewedAt });
  const meta = STATUS_META[status];
  const reachedLabel = RAIL_STAGES[state.reached].label;
  const label = state.closed
    ? `${meta.label}. Closed after reaching ${reachedLabel}.`
    : status === "APPLYING"
      ? "Applying. Viewed, application in progress."
      : `Stage ${state.reached + 1} of ${RAIL_STAGES.length}: ${reachedLabel}.`;

  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <div role="img" aria-label={label} className={cn("flex w-full gap-[3px]", size === "lg" ? "h-2.5" : "h-1.5")}>
        {RAIL_STAGES.map((stage, index) => {
          const reached = index <= state.reached;
          const inProgress = status === "APPLYING" && index === 2;
          const lastReached = index === state.reached;
          const fill = state.closed
            ? status === "REJECTED" && lastReached
              ? "var(--status-rejected)"
              : "var(--status-closed)"
            : stage.color;
          return (
            <span
              key={stage.key}
              className={cn(
                "relative flex-1 overflow-hidden rounded-[2px]",
                !reached && (state.closed ? "bg-hatch" : "bg-foreground/8"),
              )}
            >
              {reached && <span className="absolute inset-0" style={{ backgroundColor: fill }} />}
              {inProgress && (
                <span className="absolute inset-y-0 left-0 w-1/2" style={{ backgroundColor: "var(--status-applying)" }} />
              )}
            </span>
          );
        })}
      </div>
      {showLabels && (
        <div aria-hidden="true" className="flex gap-[3px] text-xs text-muted-foreground">
          {RAIL_STAGES.map((stage, index) => (
            <span
              key={stage.key}
              className={cn(
                "flex-1 truncate",
                index <= state.reached && !state.closed && "text-foreground",
                index === state.reached && "font-medium text-foreground",
              )}
            >
              {stage.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Generic small label for things that are not application statuses (roles, verification, invitation state). */
export function Tag({
  children,
  tone = "neutral",
  className,
  ...props
}: Omit<React.ComponentProps<"span">, "children"> & {
  children: React.ReactNode;
  tone?: "neutral" | "primary" | "success" | "warning" | "danger";
}) {
  return (
    <span
      data-slot="tag"
      data-tone={tone}
      className={cn(
        "inline-flex h-5.5 w-fit shrink-0 items-center gap-1 rounded-[5px] border px-1.5 text-xs font-medium whitespace-nowrap [&>svg]:size-3.5",
        tone === "neutral" && "border-border-strong bg-muted text-foreground",
        tone === "primary" && "border-primary/30 bg-primary/8 text-foreground [&>svg]:text-primary",
        tone === "success" && "border-success/35 bg-success/8 text-foreground [&>svg]:text-success",
        tone === "warning" && "border-warning/40 bg-warning/10 text-foreground [&>svg]:text-warning",
        tone === "danger" && "border-destructive/35 bg-destructive/8 text-foreground [&>svg]:text-destructive",
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}
