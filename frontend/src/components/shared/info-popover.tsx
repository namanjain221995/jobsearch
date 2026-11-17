"use client";

import type { ReactNode } from "react";
import { Info } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

/** A small "i" that opens an explanation. A popover, not a hover tooltip, so it also works by touch and keyboard. */
export function InfoPopover({
  label,
  title,
  children,
  className,
}: {
  /** What the control explains, for screen readers ("About: Completion rate"). */
  label: string;
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Popover>
      <PopoverTrigger
        aria-label={label}
        className={cn(
          "inline-grid size-5 shrink-0 place-items-center rounded-full text-muted-foreground hover:text-foreground",
          className,
        )}
      >
        <Info className="size-3.5" aria-hidden="true" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 gap-1.5 p-3 text-2sm">
        {title && <p className="font-label text-sm">{title}</p>}
        <div className="flex flex-col gap-1.5 text-muted-foreground">{children}</div>
      </PopoverContent>
    </Popover>
  );
}
