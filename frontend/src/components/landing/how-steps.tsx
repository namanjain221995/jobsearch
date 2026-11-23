"use client";

import { useEffect, useRef, useState } from "react";

/**
 * "How a search runs": the five stages as an ordered list. On wide screens a dial
 * beside the list stays in view and lights the stage being read, one segment per
 * stage, so the reader always sees where they are in the sequence.
 */

const STEPS = [
  {
    title: "Search widely",
    body: "AI agents query the open web and employers' own career sites at the same time. A search keeps going round after round until there are enough good matches or nothing new turns up.",
  },
  {
    title: "Read the source",
    body: "Each posting is read from the employer's page: the title, location, work arrangement and the full description.",
  },
  {
    title: "Verify it is live",
    body: "The page is opened and checked. Closed, expired and non-US postings are dropped before anyone sees them.",
  },
  {
    title: "Match to the resume",
    body: "Every job is scored from 0 to 100 against the candidate's resume and preferences, and each result says why it scored as it did.",
  },
  {
    title: "Assign to a recruiter",
    body: "Jobs above your minimum match score go to the recruiter responsible for the candidate, who is notified straight away. Or review the list first and assign by hand.",
  },
] as const;

const CX = 160;
const R = 128;
const GAP_DEG = 7;

function arc(index: number, total: number) {
  const span = 360 / total;
  const start = ((index * span + GAP_DEG / 2 - 90) * Math.PI) / 180;
  const end = (((index + 1) * span - GAP_DEG / 2 - 90) * Math.PI) / 180;
  const p = (a: number) => `${(CX + R * Math.cos(a)).toFixed(2)} ${(CX + R * Math.sin(a)).toFixed(2)}`;
  return `M ${p(start)} A ${R} ${R} 0 0 1 ${p(end)}`;
}

const ARCS = STEPS.map((_, i) => arc(i, STEPS.length));

export function HowSteps() {
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    const items = listRef.current?.querySelectorAll<HTMLElement>("[data-step]");
    if (!items) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) setActive(Number((entry.target as HTMLElement).dataset.step));
        }
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 },
    );
    items.forEach((item) => io.observe(item));
    return () => io.disconnect();
  }, []);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-12 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1fr)] lg:gap-20">
      <div className="hidden lg:block">
        <div className="lp-dial sticky top-28" aria-hidden="true">
          <svg viewBox="0 0 320 320" focusable="false">
            <circle cx={CX} cy={CX} r={R + 22} className="lp-dial-outer" />
            <circle cx={CX} cy={CX} r={R - 26} className="lp-dial-inner" />
            {ARCS.map((d, i) => (
              <path key={i} d={d} className="lp-dial-seg" data-on={i <= active || undefined} data-now={i === active || undefined} />
            ))}
            <g className="lp-dial-spin">
              <circle cx={CX} cy={CX - R - 22} r="3.5" className="lp-dial-satellite" />
            </g>
          </svg>
          <div className="lp-dial-center">
            <span className="font-figure text-6xl leading-none tabular-nums">{active + 1}</span>
            <span key={active} className="lp-dial-label font-display text-xl leading-tight">
              {STEPS[active].title}
            </span>
          </div>
        </div>
      </div>

      <ol ref={listRef} className="flex flex-col">
        {STEPS.map((step, i) => (
          <li key={step.title} data-step={i} data-active={i === active || undefined} className="lp-step" data-reveal>
            <span className="lp-step-num font-figure" aria-hidden="true">
              {i + 1}
            </span>
            <div>
              <h3 className="font-display text-2xl leading-tight sm:text-3xl">{step.title}</h3>
              <p className="mt-3 max-w-[34rem] text-md text-muted-foreground">{step.body}</p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
