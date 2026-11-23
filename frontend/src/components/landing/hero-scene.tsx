"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Check, UserRound } from "lucide-react";

import { useMotionPaused } from "./motion-pause";
import { useReducedMotion } from "./use-reduced-motion";

/**
 * The hero illustration: postings stream in from the left, most of them stop at one
 * of the gates (unreadable, closed, outside the US, a poor fit) and a few reach the
 * recruiter on the right. A small status card cycles through the five stages of a
 * search, and the result card fills in as each stage completes.
 *
 * Purely decorative (the text beside it says the same thing), so it is hidden from
 * assistive technology. It stops cycling while it is off screen or the tab is hidden,
 * and with reduced motion it shows the finished search and nothing moves.
 */

const STAGES = [
  { label: "Searching the web", detail: "AI agents run many queries at once" },
  { label: "Reading career sites", detail: "Opening each employer's own job page" },
  { label: "Verifying", detail: "Checking the opening is live and in the US" },
  { label: "Matching", detail: "Scoring the job against the resume" },
  { label: "Assigning", detail: "Sending the shortlist to the recruiter" },
] as const;

const W = 600;
const SOURCE_X = 34;
const OUTPUT_X = 562;
const GATES = [170, 280, 390, 500] as const;
const GATE_LABELS = ["Search", "Read", "Verify", "Match", "Assign"] as const;
const GATE_LABEL_X = [SOURCE_X, ...GATES];
const PASS = 4;
/** Where each lane's postings stop: the index of a gate, or PASS when they reach the recruiter. */
const STOPS = [1, 2, 0, 1, PASS, 2, 1, PASS, 2, 0, PASS, 1];

const LANES = STOPS.map((stop, i) => {
  const y = 40 + i * 25.5;
  const end = stop === PASS ? OUTPUT_X : GATES[stop];
  const len = end - SOURCE_X;
  const dur = len / 115 + 0.9;
  const offset = (i * 0.61) % dur;
  return { y, end, len, dur, offset, pass: stop === PASS };
});

const TICK_MS = 2300;

export function HeroScene() {
  const reduced = useReducedMotion();
  const paused = useMotionPaused();
  const [tick, setTick] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (reduced || paused) return;
    const node = ref.current;
    if (!node) return;
    let visible = true;
    let timer: number | undefined;
    const start = () => {
      if (timer === undefined && visible && !document.hidden) {
        timer = window.setInterval(() => setTick((t) => t + 1), TICK_MS);
      }
    };
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const io = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) start();
      else stop();
    });
    io.observe(node);
    const onVisibility = () => (document.hidden ? stop() : start());
    document.addEventListener("visibilitychange", onVisibility);
    start();
    return () => {
      stop();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [reduced, paused]);

  // Five stages, then one beat holding the finished result before the next search starts.
  const stage = reduced ? STAGES.length - 1 : Math.min(tick % (STAGES.length + 1), STAGES.length - 1);
  const done = reduced || tick % (STAGES.length + 1) === STAGES.length;

  return (
    <div ref={ref} className="lp-scene" data-stage={stage} data-done={done || undefined} aria-hidden="true">
      <div className="lp-scene-tilt">
        <div className="lp-board">
          <svg viewBox={`0 0 ${W} 360`} className="lp-board-svg" focusable="false">
            <defs>
              <linearGradient id="lp-gate-fill" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0" stopColor="var(--primary)" stopOpacity="0.02" />
                <stop offset="0.5" stopColor="var(--primary)" stopOpacity="0.14" />
                <stop offset="1" stopColor="var(--primary)" stopOpacity="0.02" />
              </linearGradient>
              <linearGradient id="lp-scan" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0" stopColor="var(--primary)" stopOpacity="0" />
                <stop offset="0.5" stopColor="var(--primary)" stopOpacity="0.85" />
                <stop offset="1" stopColor="var(--primary)" stopOpacity="0" />
              </linearGradient>
            </defs>

            {/* source column: the web and employers' career sites */}
            <g className="lp-source" data-gate="0">
              <rect x={SOURCE_X - 10} y="16" width="20" height="328" rx="10" />
              {Array.from({ length: 13 }, (_, i) => (
                <circle
                  key={i}
                  className="lp-source-dot"
                  cx={SOURCE_X}
                  cy={30 + i * 25}
                  r="2"
                  style={{ animationDelay: `${-(i * 0.37) % 2.4}s` }}
                />
              ))}
            </g>

            {LANES.map((lane, i) => (
              <line
                key={`l${i}`}
                className={lane.pass ? "lp-lane lp-lane-pass" : "lp-lane"}
                x1={SOURCE_X}
                x2={lane.end}
                y1={lane.y}
                y2={lane.y}
              />
            ))}

            {GATES.map((x, i) => (
              <g key={x} className="lp-gate" data-gate={i + 1}>
                <rect className="lp-gate-pane" x={x - 8} y="16" width="16" height="328" rx="8" fill="url(#lp-gate-fill)" />
                <rect
                  className="lp-gate-scan"
                  x={x - 3}
                  y="16"
                  width="6"
                  height="54"
                  rx="3"
                  fill="url(#lp-scan)"
                  style={{ animationDelay: `${-i * 0.9}s` }}
                />
              </g>
            ))}

            {/* the recruiter's queue */}
            <g className="lp-output">
              <rect x={OUTPUT_X - 6} y="60" width="12" height="240" rx="6" />
              <circle className="lp-output-halo" cx={OUTPUT_X} cy="180" r="22" />
            </g>

            {LANES.map((lane, i) =>
              [0, 0.5].map((phase) => {
                const delay = -(lane.offset + phase * lane.dur);
                const style = {
                  "--len": `${lane.len}px`,
                  "--rest": phase === 0 ? 0.35 : 0.8,
                  animationDuration: `${lane.dur}s`,
                  animationDelay: `${delay}s`,
                } as CSSProperties;
                return (
                  <g key={`p${i}-${phase}`}>
                    <circle
                      className={lane.pass ? "lp-dot lp-dot-pass" : "lp-dot"}
                      cx={SOURCE_X}
                      cy={lane.y}
                      r="3.2"
                      style={style}
                    />
                    {!lane.pass && (
                      <circle
                        className="lp-reject"
                        cx={lane.end}
                        cy={lane.y}
                        r="5"
                        style={{ animationDuration: `${lane.dur}s`, animationDelay: `${delay}s` }}
                      />
                    )}
                  </g>
                );
              }),
            )}
          </svg>

          <div className="lp-gate-labels">
            {GATE_LABELS.map((label, i) => (
              <span
                key={label}
                data-gate={i}
                data-active={i === stage || undefined}
                style={{ left: `${(GATE_LABEL_X[i] / W) * 100}%` }}
              >
                {label}
              </span>
            ))}
          </div>
        </div>

        <div className="lp-float lp-float-stage">
          <div className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-2 font-label text-2sm">
              <span className="lp-live" />
              {done ? "Search finished" : "Search running"}
            </span>
            <span className="text-xs text-muted-foreground">
              Stage {stage + 1} of {STAGES.length}
            </span>
          </div>
          <div key={done ? "done" : stage} className="lp-stage-now">
            <p className="font-display text-lg leading-tight">{done ? "Shortlist ready" : STAGES[stage].label}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {done ? "Only verified matches reach the recruiter" : STAGES[stage].detail}
            </p>
          </div>
          <ol className="lp-stage-pips">
            {STAGES.map((s, i) => (
              <li key={s.label} data-state={i < stage || done ? "done" : i === stage ? "now" : "next"} />
            ))}
          </ol>
        </div>

        <div className="lp-float lp-float-result" data-ready={stage >= 1 || done || undefined}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="font-display text-lg leading-tight">Senior data engineer</p>
              <p className="mt-0.5 text-xs text-muted-foreground">From the employer&apos;s career site</p>
            </div>
            <ScoreRing on={stage >= 3 || done} />
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="lp-chip">Austin, TX</span>
            <span className="lp-chip">Hybrid</span>
            <span className="lp-chip lp-chip-verified" data-on={stage >= 2 || done || undefined}>
              <Check className="size-3" strokeWidth={3} />
              Verified live
            </span>
          </div>
          <div className="lp-assign" data-on={stage >= 4 || done || undefined}>
            <span className="lp-avatar">
              <UserRound className="size-3.5" />
            </span>
            <span className="text-2sm">Assigned to the candidate&apos;s recruiter</span>
          </div>
        </div>
      </div>
      <p className="lp-scene-caption">Illustration of a search in progress</p>
    </div>
  );
}

function ScoreRing({ on }: { on: boolean }) {
  return (
    <span className="lp-ring" data-on={on || undefined}>
      <svg viewBox="0 0 44 44" focusable="false">
        <circle cx="22" cy="22" r="18" className="lp-ring-track" />
        <circle cx="22" cy="22" r="18" className="lp-ring-value" pathLength="100" />
      </svg>
      <span className="lp-ring-number">86</span>
    </span>
  );
}
