"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Check, Plus, Sparkles, X } from "lucide-react";

import { OptionalMark } from "@/components/shared/form-fields";
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { splitList } from "@/lib/forms";
import { MIN_TITLE_QUERY, useResumeTitles, useTitleSuggestions, type CandidateTitle } from "@/lib/job-titles";
import { cn } from "@/lib/utils";

const MAX_TITLES = 8;
const MAX_LENGTH = 120;
const TYPING_PAUSE_MS = 220;
/** Chips shown under the box before "Show all". */
const CHIPS_SHOWN = 10;

interface PickedCandidateName {
  id: string;
  name: string;
}

/** The value, a moment after it stopped changing. An empty value is passed on at once. */
function useSettled(value: string, wait: number): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), value ? wait : 0);
    return () => window.clearTimeout(timer);
  }, [value, wait]);
  return settled;
}

const words = (text: string) => text.toLowerCase().match(/[a-z0-9+#]+/g) ?? [];

/** Every typed word starts a word of the title. Keeps an answer for "dat" useful while "data e" is still being asked. */
function fitsTyped(title: string, typed: string): boolean {
  const have = words(title).map((word) => (word === "sr" ? "senior" : word === "jr" ? "junior" : word));
  return words(typed).every((want) => have.some((word) => word.startsWith(want === "sr" ? "senior" : want === "jr" ? "junior" : want)));
}

const SOURCE_NOTE: Record<CandidateTitle["source"], string> = {
  saved: "saved in the profile of",
  resume: "found in the resume of",
  model: "a related title proposed by an AI model for",
};

interface Chip {
  title: string;
  source: CandidateTitle["source"];
  /** The candidates this title was offered for. */
  names: string[];
}

/** One chip per title over all selected candidates; a title keeps the first source it came with (saved, then resume, then model). */
function toChips(candidates: Array<{ candidate: PickedCandidateName; titles: CandidateTitle[] }>): Chip[] {
  const order: CandidateTitle["source"][] = ["saved", "resume", "model"];
  const chips = new Map<string, Chip>();
  for (const source of order) {
    for (const { candidate, titles } of candidates) {
      for (const item of titles) {
        if (item.source !== source) continue;
        const key = item.title.toLowerCase();
        const chip = chips.get(key);
        if (!chip) chips.set(key, { title: item.title, source, names: [candidate.name] });
        else if (chip.source === source && !chip.names.includes(candidate.name)) chip.names.push(candidate.name);
      }
    }
  }
  return [...chips.values()];
}

/**
 * The "Job titles" box of Find Jobs: a list of titles edited as chips, exactly as before (type and
 * press Enter or a comma, paste a comma separated list, Backspace removes the last one), with two kinds
 * of help that never get in the way of typing:
 *
 * - while typing, a list of titles that match (the selected candidates' own titles, then titles real
 *   postings carry). It is a combobox: the arrow keys move through the list, Enter takes the highlighted
 *   title, Escape closes the list. With nothing highlighted Enter adds what was typed, as always.
 * - under the box, the titles the portal knows for the selected candidates, to add with one click.
 *
 * Both come from the API (lib/job-titles.ts). If it does not answer, the box is simply a box.
 */
export function JobTitlesInput({
  value,
  onChange,
  selected,
  error,
  disabled,
}: {
  value: string[];
  onChange: (value: string[]) => void;
  /** The individually selected candidates, by id (their ids are what suggestions are asked for). */
  selected: ReadonlyMap<string, PickedCandidateName>;
  error?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const listId = `${id}-suggestions`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [active, setActive] = useState(-1);
  const [showAll, setShowAll] = useState(false);
  const full = value.length >= MAX_TITLES;
  const chosen = useMemo(() => new Set(value.map((item) => item.toLowerCase())), [value]);
  const candidates = useMemo(() => [...selected.values()].map(({ id, name }) => ({ id, name })), [selected]);
  const candidateIds = useMemo(() => candidates.map((candidate) => candidate.id), [candidates]);

  /* ---------------------------------------------------------------- while typing */
  const typed = useSettled(draft.trim(), TYPING_PAUSE_MS);
  const suggestions = useTitleSuggestions(typed, candidateIds);
  const options = useMemo(() => {
    // Answers belong to the text they were asked for: nothing is offered for text that has since been cleared.
    if (draft.trim().length < MIN_TITLE_QUERY || typed.length < MIN_TITLE_QUERY || suggestions.isError) return [];
    return (suggestions.data?.items ?? []).filter((item) => !chosen.has(item.title.toLowerCase()) && fitsTyped(item.title, draft));
  }, [draft, typed, suggestions.data, suggestions.isError, chosen]);
  const open = focused && !dismissed && !full && options.length > 0;
  const activeOption = open && active >= 0 && active < options.length ? options[active] : undefined;
  const optionId = (index: number) => `${id}-option-${index}`;

  useEffect(() => {
    if (activeOption) document.getElementById(`${id}-option-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [activeOption, active, id]);

  const add = (text: string) => {
    const incoming = splitList(text).map((item) => item.slice(0, MAX_LENGTH));
    if (incoming.length === 0) return;
    const seen = new Set(chosen);
    const next = [...value];
    for (const item of incoming) {
      if (next.length >= MAX_TITLES) break;
      if (!item || seen.has(item.toLowerCase())) continue;
      seen.add(item.toLowerCase());
      next.push(item);
    }
    if (next.length !== value.length) onChange(next);
    setDraft("");
    setActive(-1);
    setDismissed(false);
  };

  const type = (text: string) => {
    setDraft(text);
    setActive(-1);
    setDismissed(false);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (options.length === 0 || full) return;
      event.preventDefault();
      if (!open) {
        setDismissed(false);
        setActive(event.key === "ArrowDown" ? 0 : options.length - 1);
        return;
      }
      const step = event.key === "ArrowDown" ? 1 : -1;
      // One step past either end is "nothing highlighted": Enter then adds the typed text again.
      setActive((current) => {
        const next = current + step;
        return next >= options.length ? -1 : next < -1 ? options.length - 1 : next;
      });
    } else if (event.key === "Escape") {
      if (!open) return;
      event.preventDefault();
      event.stopPropagation();
      setDismissed(true);
      setActive(-1);
    } else if (event.key === "Enter" || event.key === ",") {
      if (event.key === "Enter" && activeOption) {
        event.preventDefault();
        add(activeOption.title);
      } else if (draft.trim()) {
        event.preventDefault();
        add(draft);
      } else if (event.key === ",") {
        event.preventDefault();
      }
    } else if (event.key === "Backspace" && !draft && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  };

  /* ---------------------------------------------------------------- for the selected candidates */
  const known = useResumeTitles(candidateIds);
  const chips = useMemo(() => {
    if (candidateIds.length === 0 || !known.data) return [];
    const names = new Map(candidates.map((candidate) => [candidate.id, candidate]));
    return toChips(
      known.data.candidates
        .filter((entry) => names.has(entry.candidate.id))
        .map((entry) => ({ candidate: names.get(entry.candidate.id) ?? entry.candidate, titles: entry.titles })),
    );
  }, [candidateIds, candidates, known.data]);
  const fromResume = chips.some((chip) => chip.source !== "saved");
  const fromModel = chips.some((chip) => chip.source === "model");
  const visible = showAll ? chips : chips.slice(0, CHIPS_SHOWN);
  const lookingFurther = candidateIds.length > 0 && known.data?.model === "pending" && known.isFetching;

  const toggle = (title: string) => {
    if (chosen.has(title.toLowerCase())) onChange(value.filter((item) => item.toLowerCase() !== title.toLowerCase()));
    else if (!full) onChange([...value, title.slice(0, MAX_LENGTH)]);
  };

  return (
    <Field data-invalid={error ? true : undefined} data-slot="job-titles">
      <FieldLabel htmlFor={id}>
        Job titles
        <OptionalMark />
      </FieldLabel>
      <div className="relative">
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
            ref={inputRef}
            id={id}
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={activeOption ? optionId(active) : undefined}
            value={draft}
            disabled={disabled || full}
            maxLength={MAX_LENGTH * 4}
            onChange={(event) => type(event.target.value)}
            onKeyDown={onKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => {
              setFocused(false);
              setActive(-1);
              if (draft.trim()) add(draft);
            }}
            onPaste={(event) => {
              const text = event.clipboardData.getData("text");
              if (/[,\n]/.test(text)) {
                event.preventDefault();
                add(text);
              }
            }}
            placeholder={full ? "" : value.length === 0 ? "Empty: each candidate's saved titles" : ""}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-error` : `${id}-description`}
            autoComplete="off"
            className="h-6 min-w-24 flex-1 bg-transparent px-1 text-base outline-none placeholder:text-muted-foreground md:text-sm"
          />
        </div>

        {/* Always in the page, so the box can point at it; empty and hidden while there is nothing to offer. */}
        <ul
          id={listId}
          role="listbox"
          aria-label="Suggested job titles"
          hidden={!open}
          className="absolute inset-x-0 top-full z-50 mt-1 max-h-72 overflow-y-auto overscroll-contain rounded-lg border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {open &&
            options.map((option, index) => (
              <li
                key={option.title}
                id={optionId(index)}
                role="option"
                aria-selected={index === active}
                // Pressing an option must not take the focus from the box: its blur would add the half-typed text first.
                onPointerDown={(event) => event.preventDefault()}
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => index !== active && setActive(index)}
                onClick={() => {
                  add(option.title);
                  inputRef.current?.focus();
                }}
                className={cn(
                  "flex min-h-10 cursor-pointer items-center justify-between gap-3 rounded-md px-2.5 py-1.5 text-sm select-none md:min-h-8",
                  index === active && "bg-accent text-accent-foreground",
                )}
              >
                <span className="min-w-0 break-words">{option.title}</span>
                {option.source === "candidate" && (
                  <span className={cn("shrink-0 text-xs", index === active ? "text-accent-foreground/80" : "text-muted-foreground")}>
                    A selected candidate&apos;s title
                  </span>
                )}
              </li>
            ))}
        </ul>
        {/*
          What a screen reader hears about the list; sighted users see the list itself. Pinned to the corner of the
          box: left where the layout would put it, a hidden one-line text can stick out of a narrow page and make it scroll sideways.
        */}
        <span className="sr-only top-0 left-0" role="status" aria-live="polite">
          {open ? `${options.length} ${options.length === 1 ? "suggestion" : "suggestions"}. Use the up and down arrow keys, Enter to add one.` : ""}
        </span>
      </div>

      {!error && (
        <FieldDescription id={`${id}-description`}>
          Press Enter or type a comma after each title. Suggestions appear as you type; your own wording always works. Up to {MAX_TITLES}.
        </FieldDescription>
      )}
      {error && <FieldError id={`${id}-error`}>{error}</FieldError>}

      {chips.length > 0 && (
        <div role="group" aria-labelledby={`${id}-known`} data-slot="title-chips" className="flex flex-col gap-2 rounded-md border bg-muted/40 p-3">
          <p id={`${id}-known`} className="font-label text-sm">
            {fromResume ? "Suggested from the resume" : "Saved titles of the selected candidates"}
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {visible.map((chip) => {
              const added = chosen.has(chip.title.toLowerCase());
              const whose = `${SOURCE_NOTE[chip.source]} ${chip.names.join(", ")}`;
              return (
                <li key={chip.title} className="max-w-full">
                  <button
                    type="button"
                    data-source={chip.source}
                    aria-pressed={added}
                    disabled={disabled || (!added && full)}
                    title={`${chip.title}: ${whose}`}
                    aria-label={`${added ? "Remove" : "Add"} ${chip.title} (${whose})`}
                    onClick={() => toggle(chip.title)}
                    className={cn(
                      "inline-flex min-h-8 max-w-full items-center gap-1 rounded-full border px-2.5 py-1 text-left text-xs font-medium transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 md:min-h-7",
                      added ? "border-primary/40 bg-primary/10 text-foreground" : "border-border-strong bg-card hover:bg-accent",
                    )}
                  >
                    {added ? (
                      <Check className="size-3.5 shrink-0 text-primary" aria-hidden="true" />
                    ) : chip.source === "model" ? (
                      <Sparkles className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    ) : (
                      <Plus className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    )}
                    <span className="min-w-0 break-words">{chip.title}</span>
                  </button>
                </li>
              );
            })}
            {chips.length > CHIPS_SHOWN && (
              <li>
                <button
                  type="button"
                  aria-expanded={showAll}
                  onClick={() => setShowAll((current) => !current)}
                  className="inline-flex min-h-8 items-center rounded-full px-2.5 text-xs font-medium text-muted-foreground underline-offset-4 outline-none hover:text-foreground hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 md:min-h-7"
                >
                  {showAll ? "Show fewer" : `Show all ${chips.length}`}
                </button>
              </li>
            )}
          </ul>
          <p className="text-2sm text-muted-foreground">
            {fromResume
              ? "Titles saved in each candidate's profile and titles their resume names."
              : "These are the titles saved in the candidates' profiles. Their resumes name no other job title, or there is no resume."}
            {fromModel && (
              <>
                {" "}
                <Sparkles className="inline size-3.5 align-[-2px]" aria-hidden="true" /> marks a related title proposed by an AI model.
              </>
            )}{" "}
            A title you add is searched for every selected candidate.
          </p>
          {lookingFurther && (
            <p className="flex items-center gap-1.5 text-2sm text-muted-foreground" role="status">
              <Spinner className="size-3.5" />
              Looking for related titles. You can go on without them.
            </p>
          )}
        </div>
      )}
    </Field>
  );
}
