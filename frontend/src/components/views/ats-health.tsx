"use client";

import { useState } from "react";
import { Ban, CircleCheck, CircleDashed, ExternalLink, Globe, KeyRound, Layers, TriangleAlert, type LucideIcon } from "lucide-react";

import { DataTable, type Column } from "@/components/shared/data-table";
import { SegmentedFilter } from "@/components/shared/filters";
import { Panel } from "@/components/shared/page-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useAtsHealth, type AtsHealth } from "@/lib/queries";

type Platform = AtsHealth["platforms"][number];
type Provider = AtsHealth["providers"][number];
type Tone = "neutral" | "primary" | "success" | "warning" | "danger";

/** The six states the API computes, in plain words. The API decides the state; this only names it. */
const STATE: Record<Platform["state"], { label: string; tone: Tone; icon: LucideIcon }> = {
  verified_working: { label: "Verified working", tone: "success", icon: CircleCheck },
  configured_unverified: { label: "Configured, not verified yet", tone: "neutral", icon: CircleDashed },
  requires_credentials: { label: "Needs employer credentials", tone: "neutral", icon: KeyRound },
  public_discovery_only: { label: "Public pages only", tone: "primary", icon: Globe },
  temporarily_unavailable: { label: "Temporarily unavailable", tone: "warning", icon: TriangleAlert },
  unsupported: { label: "Not supported", tone: "neutral", icon: Ban },
};

const PROVIDER_STATE: Record<Provider["state"], { label: string; tone: Tone; icon: LucideIcon }> = {
  configured: { label: "Set up", tone: "success", icon: CircleCheck },
  not_configured: { label: "Not set up", tone: "warning", icon: TriangleAlert },
  switched_off: { label: "Switched off", tone: "neutral", icon: Ban },
};

function StateBadge({ platform }: { platform: Platform }) {
  const meta = STATE[platform.state];
  const Icon = meta.icon;
  return (
    <Tag tone={meta.tone} data-state={platform.state}>
      <Icon aria-hidden="true" />
      {meta.label}
    </Tag>
  );
}

/** Counts only mean something for a platform that has a reader; the others show a dash. */
const count = (platform: Platform, value: number) => (platform.reader ? value.toLocaleString() : "—");

function LastRead({ platform, now }: { platform: Platform; now: number | null }) {
  if (!platform.last_success_at) return <span className="text-muted-foreground">{platform.reader ? "Never" : "—"}</span>;
  return <span title={formatDateTime(platform.last_success_at)}>{formatRelative(platform.last_success_at, now)}</span>;
}

function Name({ platform }: { platform: Platform }) {
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
      {platform.label}
      {platform.startup && <Tag tone="primary">Startup</Tag>}
    </span>
  );
}

function Note({ platform }: { platform: Platform }) {
  return (
    <span className="flex flex-col gap-0.5 text-2sm text-muted-foreground">
      {/* Notes quote addresses and setting names; they may break anywhere, so that a long one never widens the table. */}
      <span className="wrap-anywhere">{platform.note || platform.access_note}</span>
      {platform.docs && (
        <a
          href={platform.docs}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex w-fit items-center gap-1 underline underline-offset-2 hover:text-foreground"
        >
          Vendor documentation
          <ExternalLink className="size-3" aria-hidden="true" />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      )}
    </span>
  );
}

function PlatformCard({ platform, now }: { platform: Platform; now: number | null }) {
  return (
    <div className="flex flex-col gap-2">
      <Name platform={platform} />
      <StateBadge platform={platform} />
      <p className="text-2sm text-muted-foreground">{platform.reason}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-2sm">
        <dt className="text-muted-foreground">Boards</dt>
        <dd className="tabular-nums">{count(platform, platform.boards_known)}</dd>
        <dt className="text-muted-foreground">Open postings</dt>
        <dd className="tabular-nums">{count(platform, platform.open_postings)}</dd>
        <dt className="text-muted-foreground">Last successful read</dt>
        <dd>
          <LastRead platform={platform} now={now} />
        </dd>
      </dl>
      <Note platform={platform} />
    </div>
  );
}

function Providers({ providers, onOpenIntegrations }: { providers: Provider[]; onOpenIntegrations?: () => void }) {
  return (
    <Panel
      title="Search providers"
      description="The AI models and web search services that help find postings. This shows whether each one is set up, not whether its last answer was good."
      actions={
        onOpenIntegrations && (
          <Button type="button" variant="outline" size="sm" onClick={onOpenIntegrations}>
            Open Integrations
          </Button>
        )
      }
    >
      <ul className="flex flex-col divide-y">
        {providers.map((provider) => {
          const meta = PROVIDER_STATE[provider.state];
          const Icon = meta.icon;
          return (
            <li key={provider.id} className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
              <div className="flex min-w-0 flex-1 basis-64 flex-col gap-0.5">
                <span className="text-sm font-medium">{provider.label}</span>
                <span className="text-2sm text-muted-foreground">{provider.reason}</span>
                {provider.detail && <span className="text-2sm break-words text-muted-foreground">{provider.detail}</span>}
              </div>
              <Tag tone={meta.tone} data-state={provider.state}>
                <Icon aria-hidden="true" />
                {meta.label}
              </Tag>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

const FILTERS = [
  { value: "all", label: "All platforms" },
  { value: "startup", label: "Startup platforms" },
];

/** Admin Settings, "ATS Platforms": what the portal can really read from each applicant-tracking system right now. */
export function AtsHealthPanels({ onOpenIntegrations }: { onOpenIntegrations?: () => void }) {
  const health = useAtsHealth();
  const now = useNow();
  const [filter, setFilter] = useState("all");

  if (health.isPending) return <LoadingSkeleton variant="rows" count={8} className="rounded-lg border bg-card" />;
  if (health.isError) {
    return (
      <div className="rounded-lg border bg-card">
        <ErrorState error={health.error} onRetry={() => health.refetch()} />
      </div>
    );
  }

  const { platforms, providers, window_days: days } = health.data;
  const rows = filter === "startup" ? platforms.filter((platform) => platform.startup) : platforms;
  const verified = platforms.filter((platform) => platform.state === "verified_working").length;

  const columns: Column<Platform>[] = [
    // The minimum widths add up to less than the page at 1280 px, so the note is never cut off there.
    { id: "platform", header: "Platform", cell: (platform) => <Name platform={platform} />, className: "min-w-36" },
    {
      id: "state",
      header: "State",
      className: "min-w-52",
      cell: (platform) => (
        <span className="flex flex-col gap-1">
          <StateBadge platform={platform} />
          <span className="text-2sm text-muted-foreground">{platform.reason}</span>
        </span>
      ),
    },
    { id: "boards", header: "Boards", align: "right", cell: (platform) => count(platform, platform.boards_known) },
    { id: "postings", header: "Open postings", align: "right", cell: (platform) => count(platform, platform.open_postings) },
    {
      id: "last",
      header: "Last successful read",
      className: "whitespace-nowrap",
      cell: (platform) => <LastRead platform={platform} now={now} />,
    },
    { id: "note", header: "Note", className: "min-w-64", cell: (platform) => <Note platform={platform} /> },
  ];

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-0.5">
            <h3 className="font-label text-md leading-snug">Applicant tracking systems</h3>
            <p className="max-w-[80ch] text-2sm text-muted-foreground">
              What the portal can read from each platform right now. A platform is marked as verified only after a read
              brought postings in the last {days} days. {verified.toLocaleString()} of {platforms.length.toLocaleString()} are
              verified.
            </p>
          </div>
          <SegmentedFilter label="Platforms shown" value={filter} onChange={setFilter} options={FILTERS} />
        </div>
        <DataTable
          caption="Applicant tracking systems and their state"
          columns={columns}
          rows={rows}
          rowKey={(platform) => platform.id}
          fetching={health.isFetching && health.isPlaceholderData}
          mobileCard={(platform) => <PlatformCard platform={platform} now={now} />}
          empty={
            <EmptyState
              icon={Layers}
              title={filter === "startup" ? "No startup platforms" : "No platforms"}
              description="The server did not list any platform here."
              compact
            />
          }
        />
      </div>
      {providers.length > 0 && <Providers providers={providers} onOpenIntegrations={onOpenIntegrations} />}
    </div>
  );
}
