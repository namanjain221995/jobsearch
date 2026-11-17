"use client";

import { useId, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/** A read-only value with a copy button (invitation and reset links). */
export function CopyField({ label, value, className }: { label: string; value: string; className?: string }) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Clipboard access can be refused: select the text so it can be copied by hand.
      inputRef.current?.select();
      return;
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2500);
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <Label htmlFor={id}>{label}</Label>
      <div className="flex gap-2">
        <Input
          id={id}
          ref={inputRef}
          value={value}
          readOnly
          spellCheck={false}
          className="min-w-0 flex-1 font-normal"
          onFocus={(event) => event.currentTarget.select()}
        />
        <Button type="button" variant="outline" onClick={copy} className="shrink-0">
          {copied ? <Check data-icon="inline-start" className="text-success" /> : <Copy data-icon="inline-start" />}
          <span aria-live="polite">{copied ? "Copied" : "Copy link"}</span>
        </Button>
      </div>
    </div>
  );
}
