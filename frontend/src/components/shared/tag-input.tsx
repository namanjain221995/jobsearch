"use client";

import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { X } from "lucide-react";

import { OptionalMark } from "@/components/shared/form-fields";
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { splitList } from "@/lib/forms";
import { cn } from "@/lib/utils";

/**
 * A list of short values (job titles, skills, states) edited as chips.
 * Type and press Enter or a comma to add; paste a comma separated list to add
 * several; Backspace in the empty box removes the last one.
 */
export function TagInput({
  label,
  value,
  onChange,
  placeholder,
  description,
  error,
  max,
  maxLength = 120,
  optional,
  disabled,
  className,
  transform,
}: {
  label: ReactNode;
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
  description?: ReactNode;
  error?: string;
  /** Most values the list may hold. */
  max?: number;
  maxLength?: number;
  optional?: boolean;
  disabled?: boolean;
  className?: string;
  /** Normalises each value as it is added (upper-case state codes, for example). */
  transform?: (value: string) => string;
}) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const full = max !== undefined && value.length >= max;

  const add = (text: string) => {
    const incoming = splitList(text).map((item) => (transform ? transform(item) : item).slice(0, maxLength));
    if (incoming.length === 0) return;
    const seen = new Set(value.map((item) => item.toLowerCase()));
    const next = [...value];
    for (const item of incoming) {
      if (max !== undefined && next.length >= max) break;
      if (!item || seen.has(item.toLowerCase())) continue;
      seen.add(item.toLowerCase());
      next.push(item);
    }
    if (next.length !== value.length) onChange(next);
    setDraft("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") {
      if (draft.trim()) {
        event.preventDefault();
        add(draft);
      } else if (event.key === ",") {
        event.preventDefault();
      }
    } else if (event.key === "Backspace" && !draft && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  };

  return (
    <Field data-invalid={error ? true : undefined} className={className}>
      <FieldLabel htmlFor={id}>
        {label}
        {optional && <OptionalMark />}
      </FieldLabel>
      <div
        className={cn(
          "flex min-h-8 flex-wrap items-center gap-1.5 rounded-lg border border-input bg-card px-1.5 py-1 transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input/30",
          error && "border-destructive ring-3 ring-destructive/20",
          disabled && "opacity-60",
        )}
      >
        {value.map((item) => (
          <span
            key={item}
            className="inline-flex h-5.5 max-w-full items-center gap-0.5 rounded-[5px] border border-border-strong bg-muted pr-0.5 pl-1.5 text-xs font-medium"
          >
            <span className="truncate">{item}</span>
            <button
              type="button"
              disabled={disabled}
              aria-label={`Remove ${item}`}
              onClick={() => onChange(value.filter((other) => other !== item))}
              className="grid size-4 shrink-0 place-items-center rounded-[3px] text-muted-foreground hover:bg-foreground/10 hover:text-foreground"
            >
              <X className="size-3" aria-hidden="true" />
            </button>
          </span>
        ))}
        <input
          id={id}
          value={draft}
          disabled={disabled || full}
          maxLength={maxLength * 4}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={() => draft.trim() && add(draft)}
          onPaste={(event) => {
            const text = event.clipboardData.getData("text");
            if (/[,\n]/.test(text)) {
              event.preventDefault();
              add(text);
            }
          }}
          placeholder={full ? "" : value.length === 0 ? placeholder : ""}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : description ? `${id}-description` : undefined}
          autoComplete="off"
          className="h-6 min-w-24 flex-1 bg-transparent px-1 text-base outline-none placeholder:text-muted-foreground md:text-sm"
        />
      </div>
      {description && !error && (
        <FieldDescription id={`${id}-description`}>
          {description}
          {max !== undefined && ` Up to ${max}.`}
        </FieldDescription>
      )}
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}
    </Field>
  );
}
