"use client";

import { useId } from "react";

import { DayInput } from "@/components/shared/day-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { RANGE_PRESETS } from "@/lib/date-range";
import { cn } from "@/lib/utils";

/**
 * Date range: presets first, a custom range on request. The range selects
 * assignments by the day they were assigned.
 */
export function DateRangeFilter({
  preset,
  from,
  to,
  onChange,
  today,
  className,
}: {
  preset: string;
  from: string;
  to: string;
  onChange: (patch: { range?: string; from?: string; to?: string }) => void;
  today: string | null;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("flex flex-wrap items-end gap-2", className)}>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-preset`} className="text-xs text-muted-foreground">
          Assigned in
        </label>
        <Select value={preset} onValueChange={(value) => onChange({ range: value })}>
          <SelectTrigger id={`${id}-preset`} className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RANGE_PRESETS.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {preset === "custom" && (
        <>
          <DayInput id={`${id}-from`} label="From" value={from} max={to || today || undefined} onChange={(value) => onChange({ from: value })} />
          <DayInput id={`${id}-to`} label="To" value={to} min={from || undefined} max={today || undefined} onChange={(value) => onChange({ to: value })} />
        </>
      )}
    </div>
  );
}
