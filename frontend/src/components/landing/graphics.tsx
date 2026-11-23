import type { CSSProperties } from "react";
import { Check, FileText, Lock, X } from "lucide-react";

/**
 * Illustrations for the landing page sections. All decorative: each one sits next to
 * text that says the same thing, so every one is hidden from assistive technology.
 * Server components only; the motion is CSS (see landing.css), started when the
 * surrounding `data-reveal` block scrolls into view.
 */

/* ------------------------------------------------------------------ agents orbit */

const C = 300;
const RINGS = [
  { r: 142, dur: 46, reverse: false, nodes: [{ a: 24, label: "Reader" }, { a: 204, label: "Verifier" }] },
  { r: 222, dur: 70, reverse: true, nodes: [{ a: 96, label: "Discovery" }, { a: 216, label: "Ranker" }, { a: 336, label: "Explainer" }] },
] as const;

const polar = (r: number, deg: number) => {
  const a = ((deg - 90) * Math.PI) / 180;
  return { x: +(C + r * Math.cos(a)).toFixed(2), y: +(C + r * Math.sin(a)).toFixed(2) };
};

export function AgentOrbit() {
  return (
    <div className="lp-orbit" aria-hidden="true">
      <svg viewBox="0 0 600 600" focusable="false">
        <circle cx={C} cy={C} r="284" className="lp-orbit-ring lp-orbit-ring-faint" />
        {RINGS.map((ring) => (
          <circle key={ring.r} cx={C} cy={C} r={ring.r} className="lp-orbit-ring" />
        ))}

        {/* sources: many small points on the outer ring */}
        <g className="lp-spin" style={{ "--spin-dur": "120s" } as CSSProperties}>
          {Array.from({ length: 18 }, (_, i) => {
            const p = polar(284, i * 20 + (i % 3) * 4);
            return <circle key={i} cx={p.x} cy={p.y} r={i % 4 === 0 ? 3.4 : 2.2} className="lp-orbit-source" />;
          })}
        </g>

        {RINGS.map((ring) => (
          <g
            key={`g${ring.r}`}
            className="lp-spin"
            data-reverse={ring.reverse || undefined}
            style={{ "--spin-dur": `${ring.dur}s` } as CSSProperties}
          >
            {ring.nodes.map((node, i) => {
              const p = polar(ring.r, node.a);
              return (
                <g key={node.label}>
                  <line x1={C} y1={C} x2={p.x} y2={p.y} className="lp-orbit-spoke" style={{ animationDelay: `${-i * 0.7}s` }} />
                  <g transform={`translate(${p.x} ${p.y})`}>
                    <g
                      className="lp-spin lp-counter"
                      data-reverse={!ring.reverse || undefined}
                      style={{ "--spin-dur": `${ring.dur}s` } as CSSProperties}
                    >
                      <rect x="-64" y="-21" width="128" height="42" rx="21" className="lp-orbit-node" />
                      <circle cx="-45" cy="0" r="5" className="lp-orbit-node-dot" />
                      <text x="8" y="6.5" textAnchor="middle" className="lp-orbit-label">
                        {node.label}
                      </text>
                    </g>
                  </g>
                </g>
              );
            })}
          </g>
        ))}

        <circle cx={C} cy={C} r="62" className="lp-orbit-pulse" />
        <circle cx={C} cy={C} r="62" className="lp-orbit-pulse" style={{ animationDelay: "-1.6s" }} />
        <circle cx={C} cy={C} r="62" className="lp-orbit-core" />
        <g transform={`translate(${C - 18} ${C - 34})`} className="lp-orbit-doc">
          <rect width="36" height="44" rx="5" />
          <line x1="8" x2="28" y1="12" y2="12" />
          <line x1="8" x2="24" y1="20" y2="20" />
          <line x1="8" x2="28" y1="28" y2="28" />
          <line x1="8" x2="20" y1="36" y2="36" />
        </g>
        <text x={C} y={C + 34} textAnchor="middle" className="lp-orbit-core-label">
          Resume
        </text>
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------- verification view */

export function VerifyBrowser() {
  return (
    <div className="lp-browser" aria-hidden="true">
      <div className="lp-browser-bar">
        <span className="lp-browser-dots">
          <i />
          <i />
          <i />
        </span>
        <span className="lp-browser-url">
          <Lock className="size-3" />
          careers.employer.example/jobs/senior-data-engineer
        </span>
      </div>
      <div className="lp-browser-body">
        <div className="lp-browser-scan" />
        <p className="font-display text-2xl leading-tight">Senior data engineer</p>
        <p className="mt-1 text-2sm text-muted-foreground">Austin, Texas, United States</p>
        <div className="mt-5 flex flex-col gap-2.5">
          <span className="lp-skel" style={{ width: "92%" }} />
          <span className="lp-skel" style={{ width: "86%" }} />
          <span className="lp-skel" style={{ width: "64%" }} />
          <span className="lp-skel mt-3" style={{ width: "40%" }} />
          <span className="lp-skel" style={{ width: "78%" }} />
          <span className="lp-skel" style={{ width: "70%" }} />
        </div>
        <span className="lp-browser-apply">Apply now</span>
      </div>
      <div className="lp-verdicts">
        <span className="lp-verdict" data-kind="drop">
          <X className="size-3.5" strokeWidth={2.6} />
          Posting closed, dropped
        </span>
        <span className="lp-verdict" data-kind="drop">
          <X className="size-3.5" strokeWidth={2.6} />
          Outside the US, dropped
        </span>
        <span className="lp-verdict" data-kind="keep">
          <Check className="size-3.5" strokeWidth={2.6} />
          Live and open, kept
        </span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- match score */

const RUBRIC = [
  { label: "Skills", got: 31, of: 35 },
  { label: "Title", got: 22, of: 25 },
  { label: "Experience", got: 16, of: 20 },
  { label: "Work arrangement", got: 10, of: 10 },
  { label: "Everything else", got: 7, of: 10 },
] as const;

export function MatchCard() {
  return (
    <div className="lp-match" aria-hidden="true">
      <div className="lp-match-head">
        <div className="min-w-0">
          <p className="font-display text-2xl leading-tight sm:text-3xl">Senior data engineer</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <span className="lp-chip">Austin, TX</span>
            <span className="lp-chip">Hybrid</span>
            <span className="lp-chip lp-chip-verified" data-on>
              <Check className="size-3" strokeWidth={3} />
              Verified live
            </span>
          </div>
        </div>
        <div className="lp-bigring">
          <svg viewBox="0 0 120 120" focusable="false">
            <circle cx="60" cy="60" r="52" className="lp-ring-track" />
            <circle cx="60" cy="60" r="52" className="lp-bigring-value" pathLength="100" />
          </svg>
          <span className="lp-bigring-number font-figure" />
          <span className="lp-bigring-of">of 100</span>
        </div>
      </div>

      <ul className="lp-rubric">
        {RUBRIC.map((row, i) => (
          <li key={row.label} style={{ "--i": i } as CSSProperties}>
            <span className="text-2sm">{row.label}</span>
            <span className="lp-rubric-track" style={{ flexGrow: row.of }}>
              <span style={{ width: `${(row.got / row.of) * 100}%` }} />
            </span>
            <span className="text-xs text-muted-foreground tabular-nums">
              {row.got} of {row.of}
            </span>
          </li>
        ))}
      </ul>

      <blockquote className="lp-why">
        <span className="font-label text-2sm">Why it matches</span>
        <p className="mt-1.5 text-md">
          Six years of Python and Spark against five asked for, and the hybrid schedule in Austin fits the
          candidate&apos;s preference. The posting asks for streaming experience the resume only touches on.
        </p>
      </blockquote>
    </div>
  );
}

/* -------------------------------------------------------------- teams and rails */

const ROOMS = [
  { x: 6, name: "Team A" },
  { x: 128, name: "Team B" },
  { x: 250, name: "Team C" },
] as const;

export function TeamRooms() {
  return (
    <div className="lp-rooms" aria-hidden="true">
      <svg viewBox="0 0 372 168" focusable="false">
        {ROOMS.map((room, r) => (
          <g key={room.name}>
            <rect x={room.x} y="6" width="116" height="130" rx="16" className="lp-room" />
            {[0, 1, 2, 3].map((d) => (
              <circle
                key={d}
                cx={room.x + 30 + (d % 2) * 56}
                cy={40 + Math.floor(d / 2) * 58}
                r={d === 0 ? 7 : 5.5}
                className={d === 0 ? "lp-room-lead" : "lp-room-dot"}
                style={{ animationDelay: `${-(r * 1.3 + d * 2.1)}s`, animationName: `lp-wander-${(r + d) % 3}` }}
              />
            ))}
            <text x={room.x + 58} y="158" textAnchor="middle" className="lp-room-label">
              {room.name}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

const RAILS = [
  { label: "Assigned", width: 100, tone: "var(--status-assigned)" },
  { label: "Viewed", width: 80, tone: "var(--status-viewed)" },
  { label: "Applied", width: 58, tone: "var(--status-applied)" },
  { label: "Interview", width: 34, tone: "var(--status-interviewing)" },
  { label: "Offer", width: 16, tone: "var(--status-offer)" },
] as const;

export function TrackingRails() {
  return (
    <ol className="lp-rails" aria-hidden="true">
      {RAILS.map((rail, i) => (
        <li key={rail.label} style={{ "--i": i } as CSSProperties}>
          <span className="font-display text-xl leading-none">{rail.label}</span>
          <span className="lp-rail">
            <span style={{ width: `${rail.width}%`, background: rail.tone }} />
          </span>
        </li>
      ))}
    </ol>
  );
}

/* --------------------------------------------------------------- salesforce feed */

export function SalesforceFeed() {
  return (
    <div className="lp-feed" aria-hidden="true">
      <div className="lp-feed-box">
        <span className="font-label text-2sm">Salesforce</span>
        <span className="lp-feed-row" />
        <span className="lp-feed-row" style={{ width: "70%" }} />
        <span className="lp-feed-row" style={{ width: "84%" }} />
      </div>
      <div className="lp-feed-channel">
        <span className="lp-feed-lock">
          <Lock className="size-3" />
          Read-only
        </span>
        {[0, 1, 2].map((i) => (
          <span key={i} className="lp-feed-packet" style={{ animationDelay: `${i * 1.3}s` }}>
            <FileText className="size-3.5" />
          </span>
        ))}
      </div>
      <div className="lp-feed-box lp-feed-box-in">
        <span className="font-label text-2sm">AI Job Finder</span>
        <span className="lp-feed-card">
          <span className="lp-feed-avatar" />
          <span className="flex flex-1 flex-col gap-1.5">
            <span className="lp-feed-row" style={{ width: "80%" }} />
            <span className="lp-feed-row" style={{ width: "55%" }} />
          </span>
        </span>
        <span className="lp-feed-file">
          <FileText className="size-3.5" />
          Resume
        </span>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- auto-apply form */

const FIELDS = [
  { label: "Full name", value: "Jordan Avery" },
  { label: "E-mail", value: "jordan.avery@example.com" },
  { label: "Authorized to work in the US", value: "Yes, from the candidate's answers" },
] as const;

export function AutoApplyForm() {
  return (
    <div className="lp-form" aria-hidden="true">
      <div className="flex items-center justify-between gap-3">
        <span className="font-label text-2sm">Employer application form</span>
        <span className="lp-form-badge">Dry run</span>
      </div>
      <div className="mt-4 flex flex-col gap-3">
        {FIELDS.map((field, i) => (
          <div key={field.label} className="lp-form-field" style={{ "--i": i } as CSSProperties}>
            <span className="text-xs text-muted-foreground">{field.label}</span>
            <span className="lp-form-input">
              <span className="lp-form-typed">{field.value}</span>
            </span>
          </div>
        ))}
      </div>
      <div className="lp-form-approve">
        <span className="text-2sm text-muted-foreground">Nothing is sent until an Admin approves this preview.</span>
        <span className="lp-form-button">Approve and submit</span>
      </div>
    </div>
  );
}
