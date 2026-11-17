import type { ReactNode } from "react";
import { CloudOff, FileQuestion, Lock, RefreshCw, TriangleAlert, Undo2, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ empty */

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  compact = false,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex w-full flex-col items-center justify-center text-center",
        compact ? "gap-1.5 px-4 py-8" : "gap-2 px-6 py-14",
        className,
      )}
    >
      {Icon && (
        <span
          className={cn(
            "mb-1 grid place-items-center rounded-md border border-dashed border-border-strong text-muted-foreground",
            compact ? "size-8" : "size-10",
          )}
          aria-hidden="true"
        >
          <Icon className={compact ? "size-4" : "size-5"} />
        </span>
      )}
      <p className="font-label text-sm text-foreground">{title}</p>
      {description && <p className="max-w-sm text-2sm text-pretty text-muted-foreground">{description}</p>}
      {action && <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ error */

function describeError(error: unknown): { icon: LucideIcon; title: string; description?: string } {
  if (error instanceof ApiError) {
    if (error.isForbidden) {
      return {
        icon: Lock,
        title: "Access denied",
        description: `${error.message}. If you need this, ask an Admin to change your role or team.`,
      };
    }
    if (error.isNotFound) {
      return { icon: FileQuestion, title: "Not found", description: `${error.message}. It may have been removed.` };
    }
    if (error.isGone) {
      // 410: it was this person's and a manager took it back. The API's sentence says exactly that
      // ("This job is no longer assigned to you."), so it is shown as it is, with nothing added.
      return { icon: Undo2, title: error.message };
    }
    if (error.isNetwork) {
      return { icon: CloudOff, title: "The service is not reachable", description: error.message };
    }
    return { icon: TriangleAlert, title: "This could not be loaded", description: error.message };
  }
  return {
    icon: TriangleAlert,
    title: "This could not be loaded",
    description: error instanceof Error && error.message ? error.message : "Something unexpected happened.",
  };
}

export function ErrorState({
  error,
  onRetry,
  compact = false,
  className,
  unavailable,
  action,
}: {
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
  className?: string;
  /**
   * Better words for "the API will not show this" (403 or 404) on a screen that knows what was asked for.
   * A 410 is not replaced: there the API's own sentence says what happened.
   */
  unavailable?: { title: string; description: string };
  /** Where to go instead, shown under the message. */
  action?: ReactNode;
}) {
  const denied = error instanceof ApiError && error.isForbidden;
  const gone = error instanceof ApiError && error.isGone;
  const refused = error instanceof ApiError && (error.isForbidden || error.isNotFound);
  const described = describeError(error);
  const { icon: Icon } = described;
  const { title, description } = refused && unavailable ? unavailable : described;
  return (
    <div
      role="alert"
      data-slot="error-state"
      data-denied={denied || undefined}
      data-gone={gone || undefined}
      className={cn(
        "flex w-full flex-col items-center justify-center text-center",
        compact ? "gap-1.5 px-4 py-8" : "gap-2 px-6 py-14",
        className,
      )}
    >
      <span
        className={cn(
          "mb-1 grid place-items-center rounded-md border text-destructive",
          denied || gone ? "border-border-strong text-muted-foreground" : "border-destructive/30 bg-destructive/5",
          compact ? "size-8" : "size-10",
        )}
        aria-hidden="true"
      >
        <Icon className={compact ? "size-4" : "size-5"} />
      </span>
      <p className="max-w-md font-label text-sm text-pretty text-foreground">{title}</p>
      {description && <p className="max-w-md text-2sm text-pretty text-muted-foreground">{description}</p>}
      {/* Asking again does not change an answer: no access, not there, or taken back. */}
      {onRetry && !refused && !gone && (
        <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
          <RefreshCw data-icon="inline-start" />
          Try again
        </Button>
      )}
      {action && <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ loading */

type SkeletonVariant = "rows" | "cards" | "kpi" | "chart" | "detail" | "form" | "list";

/** Placeholder shapes that match what is about to appear, so nothing jumps when data lands. */
export function LoadingSkeleton({
  variant = "rows",
  count = 5,
  className,
}: {
  variant?: SkeletonVariant;
  count?: number;
  className?: string;
}) {
  const items = Array.from({ length: count }, (_, index) => index);
  return (
    <div role="status" aria-label="Loading" aria-busy="true" data-slot="loading" className={cn("w-full", className)}>
      {variant === "rows" && (
        <div className="divide-y">
          {items.map((i) => (
            <div key={i} className="flex items-center gap-4 px-3 py-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="hidden h-4 w-1/5 sm:block" />
              <Skeleton className="hidden h-4 w-1/6 md:block" />
              <Skeleton className="ml-auto h-4 w-16" />
            </div>
          ))}
        </div>
      )}
      {variant === "list" && (
        <div className="flex flex-col gap-3">
          {items.map((i) => (
            <div key={i} className="flex items-start gap-3">
              <Skeleton className="mt-0.5 size-7 rounded-md" />
              <div className="flex flex-1 flex-col gap-1.5">
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            </div>
          ))}
        </div>
      )}
      {variant === "cards" && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((i) => (
            <div key={i} className="flex flex-col gap-3 rounded-lg border bg-card p-4">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-3 w-1/2" />
              <Skeleton className="h-2 w-full" />
              <div className="flex gap-2">
                <Skeleton className="h-5 w-16" />
                <Skeleton className="h-5 w-20" />
              </div>
            </div>
          ))}
        </div>
      )}
      {variant === "kpi" && (
        <div className="grid grid-cols-2 overflow-hidden rounded-lg border bg-card md:grid-cols-3 xl:grid-cols-6">
          {items.map((i) => (
            <div key={i} className="flex flex-col gap-3 border-b border-r p-4">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-8 w-14" />
            </div>
          ))}
        </div>
      )}
      {variant === "chart" && (
        <div className="flex h-56 items-end gap-2 px-2 pb-2">
          {[40, 65, 50, 80, 60, 90, 55, 70, 45, 75, 62, 85].map((h, i) => (
            <Skeleton key={i} className="flex-1 rounded-sm" style={{ height: `${h}%` }} />
          ))}
        </div>
      )}
      {variant === "detail" && (
        <div className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <Skeleton className="h-7 w-2/3" />
            <Skeleton className="h-4 w-1/3" />
          </div>
          <Skeleton className="h-2 w-full max-w-md" />
          <div className="grid gap-3 sm:grid-cols-2">
            {items.map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-11/12" />
            <Skeleton className="h-4 w-4/5" />
          </div>
        </div>
      )}
      {variant === "form" && (
        <div className="flex flex-col gap-5">
          {items.map((i) => (
            <div key={i} className="flex flex-col gap-2">
              <Skeleton className="h-3 w-28" />
              <Skeleton className="h-8 w-full" />
            </div>
          ))}
        </div>
      )}
      <span className="sr-only">Loading</span>
    </div>
  );
}
