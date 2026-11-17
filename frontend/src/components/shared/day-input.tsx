"use client";

import { useState } from "react";

import { Input } from "@/components/ui/input";
import { DAY_MAX, DAY_MIN, isApiDay } from "@/lib/date-range";
import { cn } from "@/lib/utils";

/**
 * One day of a date filter. The API accepts days from 2000 to 2100 and refuses
 * anything else, so a day outside that is not sent and the box says why.
 *
 * While a year is being typed the browser reports it digit by digit (0002,
 * 0020, 0202, 2026). Those half-typed years are not complained about until the
 * box is left; a complete year outside the range is, at once.
 */
export function DayInput({
  id,
  label,
  value,
  onChange,
  min,
  max,
  className,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  min?: string;
  max?: string;
  className?: string;
}) {
  const [editing, setEditing] = useState(false);
  const halfTyped = editing && value.startsWith("0");
  const refused = value !== "" && !isApiDay(value) && !halfTyped;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </label>
      <Input
        id={id}
        type="date"
        value={value}
        min={min && isApiDay(min) ? min : DAY_MIN}
        max={max && isApiDay(max) ? max : DAY_MAX}
        aria-invalid={refused || undefined}
        aria-describedby={refused ? `${id}-note` : undefined}
        onFocus={() => setEditing(true)}
        onBlur={() => setEditing(false)}
        onChange={(event) => onChange(event.target.value)}
        className="w-40"
      />
      {refused && (
        <span id={`${id}-note`} role="alert" className="max-w-40 text-xs text-destructive">
          Choose a date between 2000 and 2100.
        </span>
      )}
    </div>
  );
}
