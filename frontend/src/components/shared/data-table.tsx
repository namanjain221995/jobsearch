"use client";

import { useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

import { Pagination } from "@/components/shared/pagination";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Checkbox } from "@/components/ui/checkbox";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

export interface Column<T> {
  id: string;
  header: ReactNode;
  /** Plain-text name of the column when `header` is not text. */
  label?: string;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  /** Classes for both the header and the body cell (width, breakpoint visibility). */
  className?: string;
  /** Sort this column in the browser. Only for tables that hold every row (no server paging). */
  sortValue?: (row: T) => number | string | null | undefined;
}

export interface TableSelection<T> {
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  /** Rows that cannot be picked (already assigned, for example). */
  isSelectable?: (row: T) => boolean;
  /** Name of a row for its checkbox ("Select AI Engineer at Globex"). */
  label: (row: T) => string;
}

export interface TablePagination {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
  noun?: string;
  summary?: (range: { first: number; last: number; total: number }) => string;
}

interface DataTableProps<T> {
  /** What the table lists, for screen readers. */
  caption: string;
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  loading?: boolean;
  /** True while fresh data is on its way; the current rows stay in place, slightly dimmed. */
  fetching?: boolean;
  error?: unknown;
  onRetry?: () => void;
  /** Shown when there are no rows. */
  empty: ReactNode;
  selection?: TableSelection<T>;
  pagination?: TablePagination;
  /** On small screens each row is drawn with this instead of as a table row. */
  mobileCard?: (row: T, select: ReactNode) => ReactNode;
  /** Initial browser-side sort, for tables with `sortValue` columns. */
  defaultSort?: { id: string; direction: "asc" | "desc" };
  rowClassName?: (row: T) => string | undefined;
  className?: string;
}

function compare(a: number | string | null | undefined, b: number | string | null | undefined): number {
  if (a === b) return 0;
  if (a === null || a === undefined) return 1;
  if (b === null || b === undefined) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/**
 * One table for every list in the product: loading, empty and error states,
 * row selection, paging over the API's page / page_size / total, and a card
 * layout for small screens. Without `mobileCard` the table scrolls sideways
 * inside its own frame, never the page.
 */
export function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  loading = false,
  fetching = false,
  error,
  onRetry,
  empty,
  selection,
  pagination,
  mobileCard,
  defaultSort,
  rowClassName,
  className,
}: DataTableProps<T>) {
  const isMobile = useIsMobile();
  const [sort, setSort] = useState(defaultSort ?? null);

  const sortColumn = sort ? columns.find((column) => column.id === sort.id && column.sortValue) : undefined;
  const sorted =
    rows && sortColumn?.sortValue
      ? [...rows].sort((a, b) => {
          const result = compare(sortColumn.sortValue!(a), sortColumn.sortValue!(b));
          return sort!.direction === "asc" ? result : -result;
        })
      : rows;

  const selectable = selection && sorted ? sorted.filter((row) => selection.isSelectable?.(row) ?? true) : [];
  const selectedOnPage = selection ? selectable.filter((row) => selection.selected.has(rowKey(row))).length : 0;
  const allSelected = selectable.length > 0 && selectedOnPage === selectable.length;

  const toggleAll = () => {
    if (!selection) return;
    const next = new Set(selection.selected);
    for (const row of selectable) {
      if (allSelected) next.delete(rowKey(row));
      else next.add(rowKey(row));
    }
    selection.onChange(next);
  };
  const toggleRow = (row: T) => {
    if (!selection) return;
    const next = new Set(selection.selected);
    const key = rowKey(row);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    selection.onChange(next);
  };
  const rowCheckbox = (row: T) =>
    selection ? (
      <Checkbox
        checked={selection.selected.has(rowKey(row))}
        disabled={!(selection.isSelectable?.(row) ?? true)}
        onCheckedChange={() => toggleRow(row)}
        aria-label={selection.label(row)}
      />
    ) : null;

  const footer = pagination && (
    <Pagination
      {...pagination}
      className={cn("border-t px-3 py-2.5", !sorted?.length && !loading && "hidden")}
    />
  );

  let body: ReactNode;
  if (error && !sorted?.length) {
    body = <ErrorState error={error} onRetry={onRetry} compact />;
  } else if (loading || sorted === undefined) {
    body = <LoadingSkeleton variant="rows" count={6} />;
  } else if (sorted.length === 0) {
    body = empty;
  } else if (isMobile && mobileCard) {
    body = (
      <ul className={cn("divide-y", fetching && "opacity-60 transition-opacity")} aria-label={caption}>
        {sorted.map((row) => (
          <li key={rowKey(row)} className={cn("p-3", rowClassName?.(row))}>
            {mobileCard(row, rowCheckbox(row))}
          </li>
        ))}
      </ul>
    );
  } else {
    body = (
      <div className="w-full overflow-x-auto overscroll-x-contain" tabIndex={0} role="group" aria-label={`${caption}, scrollable`}>
        <table className={cn("w-full border-collapse text-sm", fetching && "opacity-60 transition-opacity")}>
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr className="border-b text-left">
              {selection && (
                <th scope="col" className="w-9 py-2 pr-0 pl-3">
                  <Checkbox
                    checked={allSelected ? true : selectedOnPage > 0 ? "indeterminate" : false}
                    disabled={selectable.length === 0}
                    onCheckedChange={toggleAll}
                    aria-label={allSelected ? "Clear selection on this page" : "Select all on this page"}
                  />
                </th>
              )}
              {columns.map((column) => {
                const active = sort?.id === column.id;
                const direction = active ? sort.direction : undefined;
                return (
                  <th
                    key={column.id}
                    scope="col"
                    aria-sort={
                      column.sortValue ? (direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none") : undefined
                    }
                    className={cn(
                      "h-9 px-3 font-label text-2sm whitespace-nowrap text-muted-foreground",
                      column.align === "right" && "text-right",
                      column.className,
                    )}
                  >
                    {column.sortValue ? (
                      <button
                        type="button"
                        className={cn(
                          "-mx-1 inline-flex items-center gap-1 rounded-sm px-1 py-0.5 hover:text-foreground",
                          column.align === "right" && "flex-row-reverse",
                          active && "text-foreground",
                        )}
                        onClick={() =>
                          setSort(
                            active
                              ? { id: column.id, direction: direction === "asc" ? "desc" : "asc" }
                              : { id: column.id, direction: column.align === "right" ? "desc" : "asc" },
                          )
                        }
                      >
                        {column.header}
                        {direction === "asc" ? (
                          <ArrowUp className="size-3.5" aria-hidden="true" />
                        ) : direction === "desc" ? (
                          <ArrowDown className="size-3.5" aria-hidden="true" />
                        ) : (
                          <ArrowUpDown className="size-3.5 opacity-50" aria-hidden="true" />
                        )}
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const key = rowKey(row);
              const selected = selection?.selected.has(key) ?? false;
              return (
                <tr
                  key={key}
                  data-selected={selected || undefined}
                  className={cn(
                    "border-b last:border-b-0 hover:bg-muted/60 data-selected:bg-accent/60",
                    rowClassName?.(row),
                  )}
                >
                  {selection && <td className="py-2 pr-0 pl-3 align-middle">{rowCheckbox(row)}</td>}
                  {columns.map((column) => (
                    <td
                      key={column.id}
                      className={cn(
                        "px-3 py-2.5 align-middle",
                        column.align === "right" && "text-right tabular-nums",
                        column.className,
                      )}
                    >
                      {column.cell(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div data-slot="data-table" className={cn("min-w-0 overflow-hidden rounded-lg border bg-card", className)}>
      {body}
      {footer}
    </div>
  );
}
