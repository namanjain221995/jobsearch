"use client";

import { useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { MailCheck, MailWarning } from "lucide-react";
import { z } from "@/lib/zod";

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
import { errorMessage } from "@/lib/api";
import { formatDateTime, roleLabel } from "@/lib/format";
import { applyServerErrors, isGeneralError, personNameSchema } from "@/lib/forms";
import { useCreateInvitation, useTeams } from "@/lib/queries";
import type { InvitationCreate, InvitationCreated } from "@/lib/types";

/**
 * What the creator sees right after an invitation (or a fresh link) was issued,
 * and the plain truth about how it travels.
 *
 * When the API sent the e-mail it does not hand the link to the creator as well
 * (`invite_url` is empty): whoever holds the link can set the password. Only an
 * invitation that was not e-mailed comes with a link to pass on, shown once.
 */
export function InvitationResult({ invitation }: { invitation: InvitationCreated }) {
  const hasLink = invitation.invite_url !== "";
  return (
    <div className="flex flex-col gap-4">
      {invitation.email_sent ? (
        <FormMessage tone="success">
          <span className="flex items-start gap-2">
            <MailCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              The invitation was sent by e-mail to <span className="font-medium break-all">{invitation.email}</span>.
              {!hasLink && " The link is in that e-mail only and is not shown here."}
            </span>
          </span>
        </FormMessage>
      ) : hasLink ? (
        <FormMessage tone="info">
          <span className="flex items-start gap-2">
            <MailWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
            <span>
              No e-mail was sent. Copy the link below and send it to {invitation.name} yourself. It will not be
              shown again.
            </span>
          </span>
        </FormMessage>
      ) : (
        <FormMessage>
          No e-mail was sent and the server returned no link. Use Resend on the Invitations page to create a new
          one.
        </FormMessage>
      )}
      {hasLink && <CopyField label="Invitation link" value={invitation.invite_url} />}
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
        {hasLink
          ? `The link works once. Whoever opens it chooses the password for this account, so share it only with ${invitation.name}.`
          : "The link works once. If the e-mail does not arrive, send a new one with Resend on the Invitations page."}
      </p>
    </div>
  );
}

const schema = z
  .object({
    name: personNameSchema,
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
  const failure = isGeneralError(form, create.error);

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
                    <SelectItem value="MEMBER">Member (recruiter)</SelectItem>
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
          {me.team
            ? `They will join ${me.team.name} as a Member: a recruiter who applies to jobs for the candidates you assign to them.`
            : "They will join your team as a Member: a recruiter who applies to jobs for the candidates you assign to them."}
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
 * Members and Team Leads are employees; candidates are never invited (they have no account).
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
              ? created.email_sent
                ? `${created.name} chooses a password with the link in the e-mail. The account works from then on.`
                : `The account works once ${created.name} has opened the link and chosen a password.`
              : preset.role === "TEAM_LEAD"
                ? "They get a link to choose their password. No account works until they do."
                : "For an employee who will work as a recruiter. They get a link to choose their password; no account works until they do. Candidates are added under Candidates and need no invitation."}
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
