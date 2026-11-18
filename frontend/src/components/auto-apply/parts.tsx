"use client";

import { CircleCheck, CircleDashed, CircleX, Loader, ShieldAlert, Sparkles, TriangleAlert, type LucideIcon } from "lucide-react";

import { Tag } from "@/components/shared/status-badge";
import { ANSWER_SOURCE_META, RUN_STATUS_META, type AnswerSource, type AtsInfo, type RunStatus } from "@/lib/auto-apply";

const STATUS_ICON: Partial<Record<RunStatus, LucideIcon>> = {
  preparing: Loader,
  submitting: Loader,
  needs_input: TriangleAlert,
  submitted: CircleCheck,
  failed: CircleX,
  blocked: ShieldAlert,
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const meta = RUN_STATUS_META[status];
  const Icon = STATUS_ICON[status] ?? CircleDashed;
  return (
    <Tag tone={meta.tone} data-run-status={status}>
      <Icon aria-hidden="true" className={status === "preparing" || status === "submitting" ? "animate-spin" : undefined} />
      {meta.label}
    </Tag>
  );
}

export function AnswerSourceBadge({ source }: { source: string }) {
  const meta = ANSWER_SOURCE_META[source as AnswerSource];
  return (
    <Tag tone={meta?.tone ?? "neutral"}>
      {source === "ai_draft" && <Sparkles aria-hidden="true" />}
      {meta?.label ?? source}
    </Tag>
  );
}

export function AtsBadge({ ats }: { ats: AtsInfo }) {
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <Tag>{ats.name}</Tag>
      {ats.supported ? (
        <Tag tone="success">
          <CircleCheck aria-hidden="true" />
          Supported
        </Tag>
      ) : (
        <Tag tone="warning" title={ats.reason ?? undefined}>
          <TriangleAlert aria-hidden="true" />
          Not supported
        </Tag>
      )}
    </span>
  );
}
