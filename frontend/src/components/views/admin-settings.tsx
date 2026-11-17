"use client";

import { useId } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { CircleCheck, CircleX, TriangleAlert } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

import { FormMessage, TextField } from "@/components/shared/form-fields";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AccountPanels } from "@/components/views/account-settings";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";
import { applyServerErrors } from "@/lib/forms";
import { useHealth, useIntegrations, usePortalSettings, useUpdatePortalSettings } from "@/lib/queries";
import type { Integrations, PortalSettings } from "@/lib/types";
import { useUrlState } from "@/lib/url-state";

const whole = (min: number, max: number) =>
  z
    .string()
    .trim()
    .refine((value) => {
      const number = Number(value);
      return value !== "" && Number.isInteger(number) && number >= min && number <= max;
    }, `Enter a whole number between ${min.toLocaleString()} and ${max.toLocaleString()}.`);

const schema = z
  .object({
    watermark_enabled: z.boolean(),
    deep_search_default: z.boolean(),
    default_target_count: whole(1, 500),
    default_min_match_score: whole(0, 100),
    max_members_per_search: whole(1, 50),
    max_results_per_search_run: whole(1, 5000),
    invitation_hours: whole(1, 720),
  })
  .superRefine((values, context) => {
    if (Number(values.default_target_count) > Number(values.max_results_per_search_run)) {
      context.addIssue({
        code: "custom",
        path: ["default_target_count"],
        message: "The default cannot be higher than the limit of results per search.",
      });
    }
  });
type Values = z.infer<typeof schema>;

const toValues = (settings: PortalSettings): Values => ({
  watermark_enabled: settings.watermark_enabled,
  deep_search_default: settings.deep_search_default,
  default_target_count: String(settings.default_target_count),
  default_min_match_score: String(settings.default_min_match_score),
  max_members_per_search: String(settings.max_members_per_search),
  max_results_per_search_run: String(settings.max_results_per_search_run),
  invitation_hours: String(settings.invitation_hours),
});

function SwitchRow({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description: React.ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <Switch id={id} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" aria-describedby={`${id}-description`} />
      <div className="flex flex-col gap-1">
        <Label htmlFor={id}>{label}</Label>
        <p id={`${id}-description`} className="max-w-[60ch] text-2sm text-muted-foreground">
          {description}
        </p>
      </div>
    </div>
  );
}

function PortalSettingsForm({ settings }: { settings: PortalSettings }) {
  const save = useUpdatePortalSettings();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(settings) });
  const { errors, isDirty } = form.formState;

  const onSubmit = form.handleSubmit((values) => {
    save.mutate(
      {
        watermark_enabled: values.watermark_enabled,
        deep_search_default: values.deep_search_default,
        default_target_count: Number(values.default_target_count),
        default_min_match_score: Number(values.default_min_match_score),
        max_members_per_search: Number(values.max_members_per_search),
        max_results_per_search_run: Number(values.max_results_per_search_run),
        invitation_hours: Number(values.invitation_hours),
      },
      {
        onSuccess: (saved) => {
          form.reset(toValues(saved));
          toast.success("Settings saved");
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    );
  });
  const failure = save.error && !(save.error instanceof ApiError && Object.keys(save.error.fields).length > 0);

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
      <Panel title="Member job views">
        <Controller
          control={form.control}
          name="watermark_enabled"
          render={({ field }) => (
            <SwitchRow
              checked={field.value}
              onChange={field.onChange}
              label="Show a watermark on member job views"
              description="The member's e-mail address and the current time are laid faintly over their job pages, so a copy can be traced back. It does not stop anyone from taking a screenshot."
            />
          )}
        />
      </Panel>

      <Panel title="Searches" description="Defaults and limits for every search started by a Team Lead or an Admin.">
        <div className="flex flex-col gap-5">
          <div className="grid gap-5 sm:grid-cols-2">
            <TextField
              label="Default results per member"
              inputMode="numeric"
              autoComplete="off"
              description="What the search form starts with. 1 to 500."
              error={errors.default_target_count?.message}
              {...form.register("default_target_count")}
            />
            <TextField
              label="Default minimum match score"
              inputMode="numeric"
              autoComplete="off"
              description="Jobs scoring below this are not assigned automatically. 0 to 100."
              error={errors.default_min_match_score?.message}
              {...form.register("default_min_match_score")}
            />
            <TextField
              label="Most members in one search"
              inputMode="numeric"
              autoComplete="off"
              description="1 to 50."
              error={errors.max_members_per_search?.message}
              {...form.register("max_members_per_search")}
            />
            <TextField
              label="Most results in one search"
              inputMode="numeric"
              autoComplete="off"
              description="Members times results per member may not exceed this. 1 to 5,000."
              error={errors.max_results_per_search_run?.message}
              {...form.register("max_results_per_search_run")}
            />
          </div>
          <Controller
            control={form.control}
            name="deep_search_default"
            render={({ field }) => (
              <SwitchRow
                checked={field.value}
                onChange={field.onChange}
                label="Deep search by default"
                description="Deep search adds AI web search and a browser crawl to the job-board sources. It finds more and takes longer."
              />
            )}
          />
        </div>
      </Panel>

      <Panel title="Invitations">
        <TextField
          label="Hours an invitation link stays valid"
          inputMode="numeric"
          autoComplete="off"
          description="1 to 720 (30 days). Applies to invitations created from now on."
          className="sm:max-w-80"
          error={errors.invitation_hours?.message}
          {...form.register("invitation_hours")}
        />
      </Panel>

      <div className="flex flex-wrap items-center justify-end gap-3">
        {failure && <FormMessage className="mr-auto">{errorMessage(save.error)}</FormMessage>}
        {isDirty && (
          <Button type="button" variant="ghost" onClick={() => form.reset()} disabled={save.isPending}>
            Discard changes
          </Button>
        )}
        <Button type="submit" disabled={save.isPending || !isDirty}>
          {save.isPending && <Spinner data-icon="inline-start" />}
          Save Settings
        </Button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ integrations */

function State({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return ok ? (
    <Tag tone="success">
      <CircleCheck aria-hidden="true" />
      {yes}
    </Tag>
  ) : (
    <Tag tone="warning">
      <TriangleAlert aria-hidden="true" />
      {no}
    </Tag>
  );
}

interface Source {
  name: string;
  label: string;
  kind?: string;
  note?: string;
  enabled?: boolean;
  available?: boolean;
  units?: number;
  postings?: number;
}

function Sources({ sources, settings }: { sources: Source[]; settings: PortalSettings | undefined }) {
  const save = useUpdatePortalSettings();
  const disabled = new Set(settings?.disabled_sources ?? []);

  const toggle = (source: Source, enable: boolean) => {
    const next = new Set(disabled);
    if (enable) next.delete(source.name);
    else next.add(source.name);
    save.mutate(
      { disabled_sources: [...next] },
      {
        onSuccess: () => toast.success(`${source.label} ${enable ? "switched on" : "switched off"} for searches`),
        onError: (error) => toast.error(errorMessage(error)),
      },
    );
  };

  return (
    <ul className="flex flex-col divide-y">
      {sources.map((source) => {
        const enabled = settings ? !disabled.has(source.name) : source.enabled !== false;
        return (
          <li key={source.name} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                {source.label}
                {source.kind && <Tag>{humanize(source.kind)}</Tag>}
                {source.available === false && (
                  <Tag tone="warning">
                    <TriangleAlert aria-hidden="true" />
                    Not configured on the server
                  </Tag>
                )}
              </span>
              {source.note && <span className="text-2sm text-muted-foreground">{source.note}</span>}
            </div>
            <span className="text-2sm text-muted-foreground tabular-nums">
              {(source.postings ?? 0).toLocaleString()} fresh postings from {(source.units ?? 0).toLocaleString()} reads
            </span>
            <div className="flex items-center gap-2">
              <Switch
                checked={enabled}
                disabled={!settings || save.isPending}
                onCheckedChange={(checked) => toggle(source, checked)}
                aria-label={`Use ${source.label} in searches`}
              />
              <span className="w-7 text-2sm text-muted-foreground">{enabled ? "On" : "Off"}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function Facts({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] items-center gap-x-5 gap-y-2.5 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function IntegrationsPanels({ data, settings }: { data: Integrations; settings: PortalSettings | undefined }) {
  const health = useHealth();
  const providers = data.ai_providers as Record<string, { configured?: boolean; model?: string }>;
  const worker = data.worker as { online?: boolean; last_seen?: string | number; browser?: unknown; active_searches?: number };
  const email = data.email as { backend?: string; from?: string };
  const queue = data.queue as { redis?: boolean };
  const index = data.index as { fresh_postings?: number; fresh_units?: number; stored_postings?: number };
  const lastSeen =
    typeof worker.last_seen === "number" ? formatDateTime(new Date(worker.last_seen * 1000)) : worker.last_seen ? formatDateTime(worker.last_seen) : null;
  const emailBackend = email.backend ?? "none";

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="AI providers" description="Used for resume analysis, web search and ranking. Keys are set on the server and never shown here.">
        <Facts
          rows={[
            ["OpenAI", <State key="o" ok={Boolean(providers.openai?.configured)} yes="Configured" no="No key configured" />],
            ["OpenAI model", providers.openai?.model ?? "Not set"],
            ["Claude", <State key="c" ok={Boolean(providers.claude?.configured)} yes="Configured" no="No key configured" />],
            ["Claude model", providers.claude?.model ?? "Not set"],
          ]}
        />
        {!providers.openai?.configured && !providers.claude?.configured && (
          <p className="mt-3 text-2sm text-muted-foreground">
            Without a provider, searches still run: jobs come from the job-board sources and are scored by keywords.
          </p>
        )}
      </Panel>

      <Panel title="Background services" description="Searches run in a background worker, not in the web server.">
        <Facts
          rows={[
            ["Search worker", <State key="w" ok={Boolean(worker.online)} yes="Online" no="Offline" />],
            ...(lastSeen ? ([["Worker last seen", lastSeen]] as Array<[string, React.ReactNode]>) : []),
            ...(typeof worker.active_searches === "number"
              ? ([["Searches running now", worker.active_searches.toLocaleString()]] as Array<[string, React.ReactNode]>)
              : []),
            ["Job queue (Redis)", <State key="q" ok={Boolean(queue.redis)} yes="Reachable" no="Not reachable" />],
            [
              "Database",
              health.data ? (
                <State ok={health.data.database} yes="Reachable" no="Not reachable" />
              ) : health.isError ? (
                <Tag tone="danger">
                  <CircleX aria-hidden="true" />
                  Health check failed
                </Tag>
              ) : (
                "Checking"
              ),
            ],
          ]}
        />
      </Panel>

      <Panel title="E-mail" description="How invitations and password reset links are delivered.">
        <Facts
          rows={[
            [
              "Delivery",
              emailBackend === "smtp" ? (
                <State ok yes="SMTP server" no="" />
              ) : (
                <State ok={false} yes="" no={emailBackend === "console" ? "Written to the server log only" : "Not configured"} />
              ),
            ],
            ...(email.from ? ([["Sent from", email.from]] as Array<[string, React.ReactNode]>) : []),
          ]}
        />
        {emailBackend !== "smtp" && (
          <p className="mt-3 text-2sm text-muted-foreground">
            No e-mail leaves the server. Whoever creates an invitation or a reset link is shown the link and has to pass it on.
          </p>
        )}
      </Panel>

      <Panel title="Search index" description="Postings the search engine has read from the sources and keeps to search through.">
        <Facts
          rows={[
            ["Postings stored", (index.stored_postings ?? 0).toLocaleString()],
            ["Fresh postings", (index.fresh_postings ?? 0).toLocaleString()],
            ["Fresh source reads", (index.fresh_units ?? 0).toLocaleString()],
          ]}
        />
      </Panel>

      <Panel
        title="Job sources"
        description="Where searches look for postings. Switching a source off takes effect for searches started from then on."
        className="lg:col-span-2"
      >
        <Sources sources={data.sources as Source[]} settings={settings} />
      </Panel>
    </div>
  );
}

const TAB_DEFAULTS = { tab: "portal" };
const TABS = [
  { value: "portal", label: "Portal" },
  { value: "integrations", label: "Integrations" },
  { value: "account", label: "My Account" },
];

/** Admin "Settings": portal-wide settings, the state of the integrations, and the Admin's own account. */
export function AdminSettingsView() {
  const [state, setState] = useUrlState(TAB_DEFAULTS);
  const tab = TABS.some((item) => item.value === state.tab) ? state.tab : "portal";
  const settings = usePortalSettings();
  const integrations = useIntegrations();

  return (
    <>
      <PageHeader
        title="Settings"
        description="Limits and defaults for the whole portal, the services it depends on, and your own account."
      />
      <Tabs value={tab} onValueChange={(next) => setState({ tab: next })} className="gap-5">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto border-b">
          {TABS.map((item) => (
            <TabsTrigger key={item.value} value={item.value} className="flex-none">
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="portal">
          {settings.isPending ? (
            <LoadingSkeleton variant="form" count={5} />
          ) : settings.isError ? (
            <div className="rounded-lg border bg-card">
              <ErrorState error={settings.error} onRetry={() => settings.refetch()} />
            </div>
          ) : (
            <PortalSettingsForm settings={settings.data} />
          )}
        </TabsContent>
        <TabsContent value="integrations">
          {integrations.isPending ? (
            <LoadingSkeleton variant="cards" count={4} />
          ) : integrations.isError ? (
            <div className="rounded-lg border bg-card">
              <ErrorState error={integrations.error} onRetry={() => integrations.refetch()} />
            </div>
          ) : (
            <IntegrationsPanels data={integrations.data} settings={settings.data} />
          )}
        </TabsContent>
        <TabsContent value="account">
          <AccountPanels />
        </TabsContent>
      </Tabs>
    </>
  );
}
