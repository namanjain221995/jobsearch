"use client";

import { useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { MailCheck, MailWarning } from "lucide-react";
import { z } from "zod";

import { CopyField } from "@/components/shared/copy-field";
import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, roleLabel } from "@/lib/format";
import { applyServerErrors } from "@/lib/forms";
import { useCreateInvitation, useTeams } from "@/lib/queries";
import type { InvitationCreate, InvitationCreated } from "@/lib/types";

/**
 * What the creator sees right after an invitation (or a fresh link) was issued:
 * the link, shown once, and the plain truth about whether an e-mail went out.
 */
export function InvitationResult({ invitation }: { invitation: InvitationCreated }) {
  return (
    <div className="flex flex-col gap-4">
      {invitation.email_sent ? (
        <FormMessage tone="success">
          <span className="flex items-start gap-2">
            <MailCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              An invitation e-mail was sent to <span className="font-medium">{invitation.email}</span>.
            </span>
          </span>
        </FormMessage>
      ) : (
        <FormMessage tone="info">
          <span className="flex items-start gap-2">
            <MailWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <span>
              No e-mail was sent. Copy the link below and send it to {invitation.name} yourself. It will not be
              shown again.
            </span>
          </span>
        </FormMessage>
      )}
      <CopyField label="Invitation link" value={invitation.invite_url} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">For</dt>
        <dd className="min-w-0 break-words">
          {invitation.name}, {invitation.email}
        </dd>
        <dt className="text-muted-foreground">Joins as</dt>
        <dd>
          {roleLabel(invitation.role)}
          {invitation.team ? `, ${invitation.team.name}` : ", no team yet"}
        </dd>
        <dt className="text-muted-foreground">Link valid until</dt>
        <dd>{formatDateTime(invitation.expires_at)}</dd>
      </dl>
      <p className="text-2sm text-muted-foreground">
        The link works once. Whoever opens it chooses the password for this account, so share it only with{" "}
        {invitation.name}.
      </p>
    </div>
  );
}

const schema = z
  .object({
    name: z.string().trim().min(2, "Enter the person's full name.").max(160, "Use at most 160 characters."),
    email: z.email("Enter a valid e-mail address."),
    role: z.enum(["MEMBER", "TEAM_LEAD"]),
    team_choice: z.enum(["existing", "new", "none"]),
    team_id: z.string(),
    team_name: z.string().trim().max(120, "Use at most 120 characters."),
    team_description: z.string().trim().max(2000, "Use at most 2,000 characters."),
  })
  .superRefine((values, context) => {
    if (values.team_choice === "existing" && !values.team_id) {
      context.addIssue({ code: "custom", path: ["team_id"], message: "Choose a team." });
    }
    if (values.team_choice === "new" && values.team_name.length < 2) {
      context.addIssue({ code: "custom", path: ["team_name"], message: "Enter a name for the new team." });
    }
  });
type Values = z.infer<typeof schema>;

export interface InvitationPreset {
  /** Fix the role (a Team Lead can only invite members; "Create Team Lead" only creates leads). */
  role?: "MEMBER" | "TEAM_LEAD";
  /** Preselect a team (inviting from a team's page). */
  teamId?: string;
}

function InvitationForm({ preset, onCreated, onCancel }: { preset: InvitationPreset; onCreated: (invitation: InvitationCreated) => void; onCancel: () => void }) {
  const me = useMe();
  const isAdmin = me.role === "ADMIN";
  const create = useCreateInvitation();
  const teams = useTeams(false, { enabled: isAdmin });
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: "",
      email: "",
      role: preset.role ?? "MEMBER",
      // A Team Lead invites into their own team, which the API fills in.
      team_choice: !isAdmin ? "none" : preset.role === "TEAM_LEAD" && !preset.teamId ? "new" : "existing",
      team_id: preset.teamId ?? "",
      team_name: "",
      team_description: "",
    },
  });
  const role = useWatch({ control: form.control, name: "role" });
  const teamChoice = useWatch({ control: form.control, name: "team_choice" });
  const { errors } = form.formState;

  // A team can have one Team Lead: only teams without one can take a new lead.
  const teamOptions = (teams.data ?? []).filter((team) => role === "MEMBER" || !team.team_lead);

  const onSubmit = form.handleSubmit((values) => {
    const body: InvitationCreate = {
      name: values.name,
      email: values.email,
      role: isAdmin ? values.role : "MEMBER",
      team_description: "",
    };
    if (isAdmin && values.team_choice === "existing") body.team_id = values.team_id;
    if (isAdmin && values.role === "TEAM_LEAD" && values.team_choice === "new") {
      body.team_name = values.team_name;
      body.team_description = values.team_description;
    }
    create.mutate(body, {
      onSuccess: onCreated,
      onError: (error) => {
        applyServerErrors(form, error);
      },
    });
  });
  const failure = create.error && !(create.error instanceof ApiError && Object.keys(create.error.fields).length > 0);

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {failure && <FormMessage>{errorMessage(create.error)}</FormMessage>}
      <TextField label="Full name" autoComplete="off" error={errors.name?.message} {...form.register("name")} />
      <TextField
        label="E-mail"
        type="email"
        inputMode="email"
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        error={errors.email?.message}
        {...form.register("email")}
      />

      {isAdmin && !preset.role && (
        <Controller
          control={form.control}
          name="role"
          render={({ field }) => (
            <FormField label="Role">
              {({ id }) => (
                <Select
                  value={field.value}
                  onValueChange={(value) => {
                    field.onChange(value);
                    form.setValue("team_choice", value === "TEAM_LEAD" ? "new" : "existing");
                    form.setValue("team_id", "");
                  }}
                >
                  <SelectTrigger id={id} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="MEMBER">Member</SelectItem>
                    <SelectItem value="TEAM_LEAD">Team Lead</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
      )}

      {isAdmin && role === "TEAM_LEAD" && (
        <Controller
          control={form.control}
          name="team_choice"
          render={({ field }) => (
            <FormField label="Team to lead">
              {({ id }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id={id} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="existing">A team that has no Team Lead</SelectItem>
                    <SelectItem value="new">A new team</SelectItem>
                    <SelectItem value="none">No team yet</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
      )}

      {isAdmin && teamChoice === "existing" && (
        <Controller
          control={form.control}
          name="team_id"
          render={({ field }) => (
            <FormField
              label="Team"
              error={errors.team_id?.message}
              description={
                teams.isSuccess && teamOptions.length === 0
                  ? role === "MEMBER"
                    ? "There are no active teams yet. Create a team first."
                    : "Every active team already has a Team Lead. Choose a new team instead."
                  : undefined
              }
            >
              {({ id, invalid }) => (
                <Select value={field.value} onValueChange={field.onChange} disabled={teams.isPending}>
                  <SelectTrigger id={id} aria-invalid={invalid || undefined} className="w-full">
                    <SelectValue placeholder={teams.isPending ? "Loading teams" : "Choose a team"} />
                  </SelectTrigger>
                  <SelectContent>
                    {teamOptions.map((team) => (
                      <SelectItem key={team.id} value={team.id}>
                        {team.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </FormField>
          )}
        />
      )}

      {isAdmin && role === "TEAM_LEAD" && teamChoice === "new" && (
        <>
          <TextField label="New team name" autoComplete="off" error={errors.team_name?.message} {...form.register("team_name")} />
          <TextareaField
            label="Team description"
            optional
            rows={2}
            error={errors.team_description?.message}
            {...form.register("team_description")}
          />
        </>
      )}

      {!isAdmin && (
        <p className="text-sm text-muted-foreground">
          {me.team ? `They will join ${me.team.name} as a Member.` : "They will join your team as a Member."}
        </p>
      )}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={create.isPending}>
          Cancel
        </Button>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending && <Spinner data-icon="inline-start" />}
          Create Invitation
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * Invite a person. Accounts only ever come into being this way.
 * A Team Lead invites members into their own team; an Admin can invite a member
 * into any team or create a Team Lead, optionally together with a new team.
 */
export function InvitationDialog({
  open,
  onOpenChange,
  preset = {},
  title,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preset?: InvitationPreset;
  title?: string;
}) {
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const [formKey, setFormKey] = useState(0);
  const heading = title ?? (preset.role === "TEAM_LEAD" ? "Create Team Lead" : "Add Member");

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setCreated(null);
      setFormKey((key) => key + 1);
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">{created ? "Invitation created" : heading}</DialogTitle>
          <DialogDescription>
            {created
              ? "Share the link if no e-mail went out."
              : "They get a link to choose their password. No account works until they do."}
          </DialogDescription>
        </DialogHeader>
        {created ? (
          <>
            <InvitationResult invitation={created} />
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  setCreated(null);
                  setFormKey((key) => key + 1);
                }}
              >
                Invite Another
              </Button>
              <Button onClick={() => close(false)}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <InvitationForm key={formKey} preset={preset} onCreated={setCreated} onCancel={() => close(false)} />
        )}
      </DialogContent>
    </Dialog>
  );
}
