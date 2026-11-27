import type { ReactNode } from "react";
import {
  Banknote,
  BadgeCheck,
  Building2,
  CalendarDays,
  CircleHelp,
  CircleSlash,
  Laptop,
  MapPin,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

import { PostedAge } from "@/components/jobs/posted-age";
import { Tag } from "@/components/shared/status-badge";
import { humanize } from "@/lib/format";
import type { JobSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

/** What to print when a posting does not state something. Nothing is ever guessed. */
export const NOT_STATED = "Not stated";

export function stated(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  return text && text !== "Not listed" ? text : NOT_STATED;
}

export function jobLocation(job: Pick<JobSummary, "location_text" | "city" | "state" | "country">): string {
  const text = (job.location_text ?? "").trim();
  if (text) return text;
  const parts = [job.city, job.state].map((part) => (part ?? "").trim()).filter(Boolean);
  return parts.length ? parts.join(", ") : NOT_STATED;
}

export function jobSalary(job: Pick<JobSummary, "salary_text" | "salary_min" | "salary_max" | "salary_currency">): string {
  const text = (job.salary_text ?? "").trim();
  if (text && text !== "Not listed") return text;
  const { salary_min: low, salary_max: high } = job;
  if (low || high) {
    const currency = job.salary_currency || "";
    const format = (amount: number) => amount.toLocaleString();
    const range = low && high && low !== high ? `${format(low)} to ${format(high)}` : format((low || high) as number);
    return `${currency} ${range}`.trim();
  }
  return NOT_STATED;
}

function Fact({ icon: Icon, children, muted }: { icon: LucideIcon; children: ReactNode; muted?: boolean }) {
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5", muted && "text-muted-foreground")}>
      <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="truncate">{children}</span>
    </span>
  );
}

/**
 * Location, work arrangement, pay and the posting's age, each with its own icon. Unstated facts are left out of this
 * short line, except the age: a job whose posting gives no date says so ("Posting date not stated").
 */
export function JobFactLine({ job, className, company = false }: { job: JobSummary; className?: string; company?: boolean }) {
  const location = jobLocation(job);
  const salary = jobSalary(job);
  const workMode = stated(job.work_mode);
  return (
    <div className={cn("flex flex-wrap items-center gap-x-4 gap-y-1 text-2sm", className)}>
      {company && <Fact icon={Building2}>{job.company}</Fact>}
      {location !== NOT_STATED && <Fact icon={MapPin}>{location}</Fact>}
      {workMode !== NOT_STATED && <Fact icon={Laptop}>{workMode}</Fact>}
      {salary !== NOT_STATED && <Fact icon={Banknote}>{salary}</Fact>}
      <Fact icon={CalendarDays} muted={!job.posted_at}>
        <span data-slot="posted-age"><PostedAge value={job.posted_at} /></span>
      </Fact>
    </div>
  );
}

/** How well the job fits the member, out of 100. Blank when the job was assigned without a search. */
export function MatchScore({
  score,
  className,
  size = "default",
}: {
  score: number | null | undefined;
  className?: string;
  size?: "default" | "lg";
}) {
  if (score === null || score === undefined) {
    return (
      <span className={cn("text-xs text-muted-foreground", className)} title="Assigned by hand, so no match was calculated">
        No score
      </span>
    );
  }
  const clamped = Math.max(0, Math.min(100, score));
  return (
    <span className={cn("inline-flex items-center gap-2", className)} title={`Match score ${clamped} out of 100`}>
      <span
        className={cn("font-figure leading-none text-foreground", size === "lg" ? "text-4xl" : "text-lg")}
        aria-label={`Match score ${clamped} out of 100`}
      >
        {clamped}
      </span>
      <span aria-hidden="true" className={cn("overflow-hidden rounded-full bg-foreground/8", size === "lg" ? "h-2 w-24" : "h-1 w-9")}>
        <span className="block h-full rounded-full bg-primary" style={{ width: `${clamped}%` }} />
      </span>
    </span>
  );
}

const VERIFICATION: Record<string, { label: string; tone: "success" | "neutral" | "warning" | "danger"; icon: LucideIcon }> = {
  verified: { label: "Verified", tone: "success", icon: BadgeCheck },
  unverified: { label: "Not verified", tone: "neutral", icon: CircleHelp },
  failed: { label: "Verification failed", tone: "warning", icon: TriangleAlert },
  closed: { label: "Posting closed", tone: "danger", icon: CircleSlash },
};

/** Whether the posting was checked against its source, in the API's own four states. */
export function VerificationTag({ status, className }: { status: string; className?: string }) {
  const meta = VERIFICATION[status];
  if (!meta) return <Tag className={className}>{humanize(status)}</Tag>;
  return (
    <Tag tone={meta.tone} className={className}>
      <meta.icon aria-hidden="true" />
      {meta.label}
    </Tag>
  );
}

export const ORIGIN_LABELS: Record<string, string> = {
  search: "AI search",
  manual: "Added by hand",
  import: "Imported",
};

export const SOURCE_LABELS: Record<string, string> = {
  search: "AI search",
  manual: "Assigned by hand",
  queue: "From the team queue",
};
