"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { Mail, Phone } from "lucide-react";

import { CandidateInactiveTag, ResumeTag } from "@/components/candidates/candidate-meta";
import { SourceTag } from "@/components/candidates/salesforce-panel";
import { formatDate } from "@/lib/format";
import { memberCandidateHref } from "@/lib/member-candidates";
import type { AssignmentStats, Candidate } from "@/lib/types";
import { cn } from "@/lib/utils";

const NOT_SET = "Not set";

/** "Data Engineer, ML Engineer +2": the first entries in full, the rest as a count. */
function shortList(items: string[], keep: number): string {
  if (items.length === 0) return NOT_SET;
  const shown = items.slice(0, keep).join(", ");
  return items.length > keep ? `${shown} +${items.length - keep}` : shown;
}

/** E-mail and phone as links a member can use straight away. Nothing is printed for a detail that is not on record. */
export function CandidateContact({
  candidate,
  className,
  raised = false,
}: {
  candidate: Pick<Candidate, "email" | "phone">;
  className?: string;
  /** Inside a card that is one big link: keeps these links clickable above it. */
  raised?: boolean;
}) {
  if (!candidate.email && !candidate.phone) {
    return <p className={cn("text-muted-foreground", className)}>No contact details on record</p>;
  }
  const link = cn("min-w-0 rounded-sm break-all underline-offset-4 hover:underline", raised && "relative z-[1]");
  return (
    <ul className={cn("flex min-w-0 flex-col gap-1", className)}>
      {candidate.email && (
        <li className="flex min-w-0 items-start gap-1.5">
          <Mail className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <a href={`mailto:${candidate.email}`} className={link}>
            <span className="sr-only">E-mail: </span>
            {candidate.email}
          </a>
        </li>
      )}
      {candidate.phone && (
        <li className="flex min-w-0 items-start gap-1.5">
          <Phone className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <a href={`tel:${candidate.phone.replace(/[^\d+]/g, "")}`} className={link}>
            <span className="sr-only">Phone: </span>
            {candidate.phone}
          </a>
        </li>
      )}
    </ul>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** The profile facts that decide which jobs fit. They come with the candidate itself (`profile_summary`). */
function ProfileSummary({ summary }: { summary: Candidate["profile_summary"] }) {
  const muted = (text: string) => (text === NOT_SET ? <span className="text-muted-foreground">{text}</span> : text);
  const years = summary?.years_experience;
  const experience = years === null || years === undefined ? NOT_SET : `${years} ${years === 1 ? "year" : "years"}`;
  return (
    <dl className="flex flex-col gap-1.5 text-2sm">
      <Fact label="Job titles">{muted(shortList(summary?.desired_titles ?? [], 2))}</Fact>
      <Fact label="Experience">{muted(experience)}</Fact>
      <Fact label="Authorization">{muted(summary?.work_authorization || NOT_SET)}</Fact>
      <Fact label="States">{muted(shortList(summary?.preferred_states ?? [], 6))}</Fact>
    </dl>
  );
}

/** Assigned, not applied, applied, interviews and offers of one candidate. */
export function CandidateCounts({ stats, className }: { stats: AssignmentStats; className?: string }) {
  const cells: Array<[string, number]> = [
    ["Assigned", stats.assigned],
    ["Not applied", stats.not_applied],
    ["Applied", stats.applied],
    ["Interviews", stats.interviews],
    ["Offers", stats.offers],
  ];
  return (
    <dl className={cn("grid grid-cols-3 gap-x-3 gap-y-2", className)}>
      {cells.map(([label, value]) => (
        <div key={label} className="flex min-w-0 flex-col">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="font-figure text-xl leading-tight">{value.toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * One of the member's candidates as a card: who they are, how to reach them, what they are
 * looking for, whether the resume is in, and how far the work on their jobs has come.
 * The whole card opens the candidate.
 */
export function CandidateCard({ candidate }: { candidate: Candidate }) {
  const newestJob = candidate.last_assigned_at;
  return (
    <article
      data-slot="candidate-card"
      className="group relative flex h-full flex-col gap-3.5 rounded-lg border bg-card p-4 transition-colors hover:border-border-strong has-[h3_a:focus-visible]:border-ring has-[h3_a:focus-visible]:ring-3 has-[h3_a:focus-visible]:ring-ring/40"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <h3 className="min-w-0 font-label text-lg leading-snug break-words">
          <Link href={memberCandidateHref(candidate.id)} className="outline-none after:absolute after:inset-0 after:rounded-lg">
            {candidate.name}
          </Link>
        </h3>
        <div className="flex flex-wrap items-center gap-1.5">
          <CandidateInactiveTag candidate={candidate} />
          <ResumeTag hasResume={Boolean(candidate.has_resume)} short />
          <SourceTag candidate={candidate} />
        </div>
      </div>
      <CandidateContact candidate={candidate} raised className="text-2sm" />
      <div className="border-t pt-3">
        <ProfileSummary summary={candidate.profile_summary} />
      </div>
      <div className="mt-auto flex flex-col gap-3 border-t pt-3">
        <CandidateCounts stats={candidate.stats} />
        <div className="text-xs text-muted-foreground">
          {newestJob ? `Newest job assigned ${formatDate(newestJob)}` : "No jobs assigned yet"}
        </div>
      </div>
    </article>
  );
}
