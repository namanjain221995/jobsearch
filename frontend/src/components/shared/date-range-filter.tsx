"use client";

import { useId } from "react";

import { Input } from "@/components/ui/input";
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
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-from`} className="text-xs text-muted-foreground">
              From
            </label>
            <Input
              id={`${id}-from`}
              type="date"
              value={from}
              max={to || today || undefined}
              onChange={(event) => onChange({ from: event.target.value })}
              className="w-40"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-to`} className="text-xs text-muted-foreground">
              To
            </label>
            <Input
              id={`${id}-to`}
              type="date"
              value={to}
              min={from || undefined}
              max={today || undefined}
              onChange={(event) => onChange({ to: event.target.value })}
              className="w-40"
            />
          </div>
        </>
      )}
    </div>
  );
}
