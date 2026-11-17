import Link from "next/link";
import type { ReactNode } from "react";

import { InfoPopover } from "@/components/shared/info-popover";
import { formatCompact } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A row of figures drawn as one ruled ledger strip instead of separate cards:
 * the numbers belong together, and the hairlines between them say so.
 */
export function KpiStrip({ children, className, label }: { children: ReactNode; className?: string; label: string }) {
  return (
    <dl
      aria-label={label}
      className={cn(
        "grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border md:grid-cols-3 xl:grid-cols-6",
        className,
      )}
    >
      {children}
    </dl>
  );
}

export function KPIStatCard({
  label,
  value,
  definition,
  color,
  detail,
  href,
  loading = false,
  className,
}: {
  label: string;
  /** A number is formatted for you (1,284, then 12.9K); pass a string to control it. */
  value: number | string | null | undefined;
  /** What the figure counts, in the words of the API. */
  definition?: string;
  /** The colour this figure has in the charts on the same screen. */
  color?: string;
  /** A second line under the figure, such as a rate. */
  detail?: ReactNode;
  /** Makes the figure a link to the list it counts. */
  href?: string;
  loading?: boolean;
  className?: string;
}) {
  const figure = typeof value === "number" ? formatCompact(value) : (value ?? "—");
  const body = (
    <span className="font-figure text-4xl leading-none text-foreground">
      {loading ? <span className="inline-block h-8 w-12 animate-pulse rounded-sm bg-muted align-bottom" /> : figure}
    </span>
  );
  return (
    <div className={cn("flex min-w-0 flex-col gap-2.5 bg-card px-4 py-3.5", className)}>
      <dt className="flex items-start justify-between gap-1 text-2sm text-muted-foreground">
        <span className="flex min-w-0 items-center gap-1.5">
          {color && (
            <span aria-hidden="true" className="size-2 shrink-0 rounded-[2px]" style={{ backgroundColor: color }} />
          )}
          <span className="leading-tight">{label}</span>
        </span>
        {definition && (
          <InfoPopover label={`About: ${label}`} title={label} className="-my-0.5 -mr-1.5">
            <p>{definition}</p>
          </InfoPopover>
        )}
      </dt>
      <dd className="flex flex-col gap-1">
        {href && !loading ? (
          <Link href={href} className="w-fit rounded-sm underline-offset-4 hover:underline" aria-label={`${label}: ${figure}. Open the list.`}>
            {body}
          </Link>
        ) : (
          body
        )}
        {detail && <span className="text-xs text-muted-foreground">{detail}</span>}
      </dd>
    </div>
  );
}
