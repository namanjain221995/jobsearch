"use client";

import { useId, useState } from "react";
import { ChevronDown } from "lucide-react";

import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Label } from "@/components/ui/label";
import type { SearchOptions } from "@/lib/types";
import { cn } from "@/lib/utils";

export type SearchPlatform = SearchOptions["platforms"][number];

/** What the server says about a platform it cannot read through a board reader. */
function CoverageMark({ platform }: { platform: SearchPlatform }) {
  if (platform.coverage === "full") return null;
  return (
    <Tag tone="warning" title={platform.hint || undefined}>
      {platform.coverage === "limited" ? "Limited coverage" : "Cannot be searched"}
    </Tag>
  );
}

/**
 * Limits a search to chosen applicant-tracking platforms. Nothing ticked means
 * every platform and every other source. The list is closed until it is asked
 * for; the line on the button always says what is chosen.
 */
export function PlatformFilter({
  platforms,
  value,
  onChange,
}: {
  platforms: SearchPlatform[];
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const chosen = platforms.filter((platform) => value.includes(platform.id));
  const summary =
    chosen.length === 0
      ? "All platforms"
      : chosen.length <= 3
        ? chosen.map((platform) => platform.label).join(", ")
        : `${chosen.length} platforms`;

  const toggle = (platformId: string, on: boolean) => {
    const next = new Set(value);
    if (on) next.add(platformId);
    else next.delete(platformId);
    // Kept in the server's order, so the same choice always reads the same.
    onChange(platforms.filter((platform) => next.has(platform.id)).map((platform) => platform.id));
  };

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="flex flex-col gap-2">
      <Label htmlFor={`${id}-trigger`}>ATS platforms</Label>
      <CollapsibleTrigger
        id={`${id}-trigger`}
        aria-describedby={`${id}-help`}
        className="group flex h-9 w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 text-left text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 sm:max-w-80 dark:bg-input/30"
      >
        <span className="truncate" data-slot="platform-summary">
          {summary}
        </span>
        <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" aria-hidden="true" />
      </CollapsibleTrigger>
      <p id={`${id}-help`} className="text-2sm text-muted-foreground">
        Leave it at all platforms to search everywhere. Choosing platforms leaves out job boards and every other
        site.
      </p>
      <CollapsibleContent>
        <div className="flex flex-col gap-2 rounded-md border">
          <ul className="grid divide-y sm:grid-cols-2 sm:divide-y-0">
            {platforms.map((platform) => {
              const inputId = `${id}-${platform.id}`;
              const checked = value.includes(platform.id);
              return (
                <li key={platform.id} data-platform={platform.id} data-coverage={platform.coverage} className="min-w-0">
                  <label
                    htmlFor={inputId}
                    className={cn("flex cursor-pointer items-start gap-3 px-3 py-2 hover:bg-muted/60", checked && "bg-accent/60")}
                  >
                    <Checkbox
                      id={inputId}
                      className="mt-0.5"
                      checked={checked}
                      onCheckedChange={(state) => toggle(platform.id, state === true)}
                    />
                    <span className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="text-sm font-medium">{platform.label}</span>
                        {platform.startup && <Tag>Startup</Tag>}
                        <CoverageMark platform={platform} />
                      </span>
                      {platform.coverage !== "full" && platform.hint && (
                        <span className="text-xs break-words text-muted-foreground">{platform.hint}</span>
                      )}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
          <div className="flex items-center justify-between gap-2 border-t px-3 py-1.5">
            <span className="text-2sm text-muted-foreground">
              {chosen.length === 0 ? "Nothing chosen: all platforms" : `${chosen.length} chosen`}
            </span>
            <Button type="button" variant="ghost" size="sm" disabled={chosen.length === 0} onClick={() => onChange([])}>
              All platforms
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
