"use client";

import { useId } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

export const PAGE_SIZES = [10, 25, 50, 100];

/** Paging over the API's page / page_size / total. */
export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizes = PAGE_SIZES,
  noun = "rows",
  summary,
  className,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
  pageSizes?: number[];
  /** What is being counted ("jobs", "members"). */
  noun?: string;
  /** Replaces the "1 to 25 of 60" sentence, for lists whose page is filtered further after paging. */
  summary?: (range: { first: number; last: number; total: number }) => string;
  className?: string;
}) {
  const selectId = useId();
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, page), pageCount);
  const first = total === 0 ? 0 : (current - 1) * pageSize + 1;
  const last = Math.min(total, current * pageSize);

  return (
    <nav
      aria-label={`Pages of ${noun}`}
      className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-2sm text-muted-foreground", className)}
    >
      <p aria-live="polite" className="tabular-nums">
        {summary
          ? summary({ first, last, total })
          : total === 0
            ? `No ${noun}`
            : `${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()} ${noun}`}
      </p>
      <div className="flex items-center gap-3">
        {onPageSizeChange && (
          <div className="hidden items-center gap-2 sm:flex">
            <label htmlFor={selectId}>Per page</label>
            <Select value={String(pageSize)} onValueChange={(value) => onPageSizeChange(Number(value))}>
              <SelectTrigger id={selectId} size="sm" className="w-18">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {pageSizes.map((size) => (
                  <SelectItem key={size} value={String(size)}>
                    {size}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        <div className="flex items-center gap-1.5">
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Previous page"
            disabled={current <= 1}
            onClick={() => onPageChange(current - 1)}
          >
            <ChevronLeft />
          </Button>
          <span className="min-w-20 text-center text-foreground tabular-nums">
            Page {current} of {pageCount}
          </span>
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Next page"
            disabled={current >= pageCount}
            onClick={() => onPageChange(current + 1)}
          >
            <ChevronRight />
          </Button>
        </div>
      </div>
    </nav>
  );
}
