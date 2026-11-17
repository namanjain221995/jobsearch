"use client";

import type { ReactNode } from "react";
import { FilterX } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";

/** One row of controls above the list or charts they filter. */
export function FilterBar({
  children,
  onClear,
  canClear = false,
  className,
}: {
  children: ReactNode;
  /** Shown while any filter differs from its default. */
  onClear?: () => void;
  canClear?: boolean;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label="Filters"
      className={cn("mb-4 flex flex-wrap items-end gap-2", className)}
    >
      {children}
      {onClear && canClear && (
        <Button variant="ghost" onClick={onClear} className="text-muted-foreground">
          <FilterX data-icon="inline-start" />
          Clear filters
        </Button>
      )}
    </div>
  );
}

export interface Option {
  value: string;
  label: string;
}

/** Radix Select cannot hold an empty value, so "no filter" travels as this token. */
const ALL = "__all__";

/**
 * A select used as a filter. Its first entry ("All statuses") stands for no
 * filter and is reported as an empty string.
 */
export function FilterSelect({
  label,
  value,
  onChange,
  options,
  allLabel,
  className,
}: {
  /** Accessible name; the visible text is the chosen option. */
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Option[];
  /** Text of the "no filter" entry. Leave out for a select that always has a value (sorting). */
  allLabel?: string;
  className?: string;
}) {
  return (
    <Select value={value || (allLabel ? ALL : undefined)} onValueChange={(next) => onChange(next === ALL ? "" : next)}>
      <SelectTrigger aria-label={label} className={cn("w-full min-w-36 sm:w-auto", className)}>
        <SelectValue placeholder={allLabel ?? label} />
      </SelectTrigger>
      <SelectContent>
        {allLabel && <SelectItem value={ALL}>{allLabel}</SelectItem>}
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** Two or three mutually exclusive views of the same list (All / Not applied / Applied). */
export function SegmentedFilter({
  label,
  value,
  onChange,
  options,
  className,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: ReactNode }>;
  className?: string;
}) {
  return (
    <ToggleGroup
      type="single"
      variant="outline"
      spacing={0}
      aria-label={label}
      value={value}
      // Pressing the active segment again would clear it; one segment always stays chosen.
      onValueChange={(next) => next !== "" && next !== value && onChange(next)}
      className={cn("bg-card", className)}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          className="px-3! data-[state=on]:bg-accent data-[state=on]:text-foreground"
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
