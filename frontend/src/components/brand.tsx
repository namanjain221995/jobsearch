import { cn } from "@/lib/utils";

/**
 * The mark is the product in one glyph: three bars narrowing like a hiring
 * funnel, each drawn with more ink than the one before.
 */
export function BrandMark({ className, title }: { className?: string; title?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={cn("size-6 shrink-0", className)}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <rect x="2" y="3.5" width="20" height="4.5" rx="1.4" fill="var(--status-viewed)" />
      <rect x="2" y="9.75" width="13.5" height="4.5" rx="1.4" fill="var(--status-applied)" />
      <rect x="2" y="16" width="7.5" height="4.5" rx="1.4" fill="var(--status-interviewing)" />
    </svg>
  );
}

export function BrandLockup({ className, compact = false }: { className?: string; compact?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <BrandMark />
      {!compact && <span className="font-display text-lg leading-none whitespace-nowrap">AI Job Finder</span>}
    </span>
  );
}
