"use client";

import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";

import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { cn } from "@/lib/utils";

/**
 * A search box that reports what was typed after a short pause, so a list is not
 * refetched on every keystroke. Follows `value` when it changes from outside
 * (filters cleared, browser back).
 */
export function SearchInput({
  value,
  onChange,
  label,
  placeholder,
  delay = 300,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  /** Accessible name of the box ("Search jobs"). */
  label: string;
  placeholder?: string;
  delay?: number;
  className?: string;
}) {
  const [text, setText] = useState(value);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    setText(value);
  }

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const emit = (next: string, immediately = false) => {
    setText(next);
    if (timer.current) clearTimeout(timer.current);
    if (immediately) {
      setSeen(next);
      onChange(next);
      return;
    }
    timer.current = setTimeout(() => {
      setSeen(next);
      onChange(next);
    }, delay);
  };

  return (
    <InputGroup className={cn("w-full sm:w-64", className)}>
      <InputGroupAddon>
        <Search aria-hidden="true" />
      </InputGroupAddon>
      <InputGroupInput
        type="search"
        aria-label={label}
        placeholder={placeholder ?? label}
        value={text}
        maxLength={100}
        onChange={(event) => emit(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") emit(event.currentTarget.value, true);
        }}
        className="[&::-webkit-search-cancel-button]:hidden"
      />
      {text && (
        <InputGroupAddon align="inline-end">
          <InputGroupButton size="icon-xs" aria-label="Clear search" onClick={() => emit("", true)}>
            <X />
          </InputGroupButton>
        </InputGroupAddon>
      )}
    </InputGroup>
  );
}
