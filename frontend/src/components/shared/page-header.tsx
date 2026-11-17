import Link from "next/link";
import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";

import { cn } from "@/lib/utils";

/** The title block every screen opens with: where you are, one line on what it is for, and the main action. */
export function PageHeader({
  title,
  description,
  actions,
  back,
  meta,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** A way back up, for detail screens. */
  back?: { href: string; label: string };
  /** Badges or facts that belong beside the title. */
  meta?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3 md:mb-8", className)}>
      {back && (
        <Link
          href={back.href}
          className="inline-flex w-fit items-center gap-1.5 rounded-sm text-2sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" aria-hidden="true" />
          {back.label}
        </Link>
      )}
      <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="font-display text-2xl leading-tight break-words md:text-3xl">{title}</h1>
            {meta}
          </div>
          {description && <p className="max-w-[68ch] text-md text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

/** A titled region inside a page. */
export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("flex flex-col gap-3", className)}>
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="font-display text-xl leading-tight">{title}</h2>
          {description && <p className="max-w-[68ch] text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** A bordered surface for a chart, a table or a group of facts. */
export function Panel({
  title,
  description,
  actions,
  info,
  children,
  flush = false,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** An info control placed right after the title. */
  info?: ReactNode;
  children: ReactNode;
  /** No padding around the body (tables bring their own). */
  flush?: boolean;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("flex min-w-0 flex-col rounded-lg border bg-card", className)}>
      {(title || actions) && (
        <div className="flex flex-col gap-0.5 px-4 pt-3.5 pb-1">
          <div className="flex min-h-6 items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-1">
              <h3 className="font-label text-md leading-snug">{title}</h3>
              {info}
            </div>
            {actions && <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">{actions}</div>}
          </div>
          {description && <p className="text-2sm text-muted-foreground">{description}</p>}
        </div>
      )}
      <div className={cn("min-w-0 flex-1", !flush && "p-4", !flush && title ? "pt-3" : "", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}
