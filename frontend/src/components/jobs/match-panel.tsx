import { Check, Minus, ShieldAlert, ShieldCheck, ShieldQuestion, type LucideIcon } from "lucide-react";

import { MatchScore } from "@/components/jobs/job-meta";
import { InfoPopover } from "@/components/shared/info-popover";
import { Tag } from "@/components/shared/status-badge";
import { humanize } from "@/lib/format";
import type { MatchExplanation } from "@/lib/types";

/** The five parts of a match score and how many points each can contribute (they add up to 100). */
const BREAKDOWN: Array<{ key: string; label: string; max: number }> = [
  { key: "skills", label: "Skills", max: 35 },
  { key: "title", label: "Job title", max: 25 },
  { key: "experience", label: "Experience", max: 20 },
  { key: "arrangement", label: "Work arrangement", max: 10 },
  { key: "additional", label: "Additional experience", max: 10 },
];

const AUTHORIZATION: Record<string, { icon: LucideIcon; tone: "success" | "warning" | "neutral" }> = {
  Eligible: { icon: ShieldCheck, tone: "success" },
  "Potential mismatch": { icon: ShieldAlert, tone: "warning" },
  Unknown: { icon: ShieldQuestion, tone: "neutral" },
};

function SkillList({ title, skills, matched }: { title: string; skills: string[]; matched: boolean }) {
  const Icon = matched ? Check : Minus;
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-2sm text-muted-foreground">
        {title} <span className="tabular-nums">({skills.length})</span>
      </h4>
      {skills.length === 0 ? (
        <p className="text-sm text-muted-foreground">{matched ? "None found in the resume." : "None. Every named skill is covered."}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {skills.map((skill) => (
            <li key={skill}>
              <Tag tone={matched ? "primary" : "neutral"}>
                <Icon aria-hidden="true" />
                {skill}
              </Tag>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Why a job was matched to a member: the score, what it is made of, the skills
 * that line up and the ones that do not, and what the posting says about work
 * authorization. Where the posting says nothing, the panel says so.
 */
export function MatchPanel({ match, score }: { match: MatchExplanation; score: number | null | undefined }) {
  const hasScore = score !== null && score !== undefined;
  const breakdown = match.score_breakdown as Record<string, unknown>;
  const known = BREAKDOWN.filter((part) => typeof breakdown[part.key] === "number");
  const extra = Object.entries(breakdown).filter(
    ([key, value]) => typeof value === "number" && !BREAKDOWN.some((part) => part.key === key),
  ) as Array<[string, number]>;
  const authorization = AUTHORIZATION[match.authorization_status] ?? AUTHORIZATION.Unknown;
  const hasDetail = known.length > 0 || match.matching_skills.length > 0 || match.missing_skills.length > 0 || match.reason;

  if (!hasScore && !hasDetail) {
    return (
      <p className="text-sm text-muted-foreground">
        This job was assigned by hand, so no match was calculated. Run a search for the member to get a score and
        a skills comparison.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div className="flex flex-col gap-1.5">
          <span className="flex items-center gap-1 text-2sm text-muted-foreground">
            Match score
            <InfoPopover label="About the match score" title="Match score">
              <p>
                Out of 100: skills 35, job title 25, experience 20, work arrangement 10, additional experience 10.
              </p>
              <p>Citizenship, clearance and sponsorship are filters. They never add to the score.</p>
            </InfoPopover>
          </span>
          <MatchScore score={score} size="lg" />
        </div>
        {match.score_source && (
          <Tag>
            {match.score_source === "ai"
              ? "Scored by AI"
              : match.score_source === "heuristic"
                ? "Keyword-based score"
                : humanize(match.score_source)}
          </Tag>
        )}
      </div>

      {match.reason && <p className="max-w-[72ch] text-md">{match.reason}</p>}

      {(known.length > 0 || extra.length > 0) && (
        <div className="flex flex-col gap-2.5">
          <h4 className="text-2sm text-muted-foreground">Score breakdown</h4>
          <ul className="flex max-w-xl flex-col gap-2">
            {known.map((part) => {
              const value = breakdown[part.key] as number;
              return (
                <li key={part.key} className="grid grid-cols-[9.5rem_1fr_3.25rem] items-center gap-3 text-sm">
                  <span>{part.label}</span>
                  <span className="h-2 overflow-hidden rounded-[3px] bg-foreground/8" aria-hidden="true">
                    <span
                      className="block h-full rounded-[3px] bg-primary"
                      style={{ width: `${Math.max(0, Math.min(100, (100 * value) / part.max))}%` }}
                    />
                  </span>
                  <span className="text-right tabular-nums">
                    <span className="font-semibold">{value}</span>
                    <span className="text-muted-foreground">/{part.max}</span>
                  </span>
                </li>
              );
            })}
            {extra.map(([key, value]) => (
              <li key={key} className="flex items-center justify-between gap-3 text-sm">
                <span>{humanize(key)}</span>
                <span className="font-semibold tabular-nums">{value}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid gap-6 sm:grid-cols-2">
        <SkillList title="Matching skills" skills={match.matching_skills} matched />
        <SkillList title="Missing skills" skills={match.missing_skills} matched={false} />
      </div>

      <dl className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <dt className="text-2sm text-muted-foreground">Experience comparison</dt>
          <dd className="text-sm">{match.experience_comparison || "Not stated"}</dd>
        </div>
        <div className="flex flex-col gap-1.5">
          <dt className="text-2sm text-muted-foreground">Work authorization assessment</dt>
          <dd className="flex flex-col items-start gap-1.5 text-sm">
            <Tag tone={authorization.tone}>
              <authorization.icon aria-hidden="true" />
              {match.authorization_status || "Unknown"}
            </Tag>
            <span>{match.eligibility_note || "Not specified"}</span>
          </dd>
        </div>
      </dl>
    </div>
  );
}
