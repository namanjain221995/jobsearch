"use client";

import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { FileSignature, ShieldCheck, ShieldOff } from "lucide-react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { FormField, FormMessage, TextareaField, TextField } from "@/components/shared/form-fields";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import {
  activeConsent,
  CONSENT_METHODS,
  consentMethodLabel,
  useConsents,
  useRecordConsent,
  useRevokeConsent,
  type Consent,
  type ConsentMethod,
} from "@/lib/auto-apply";
import { formatDateTime } from "@/lib/format";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { z } from "@/lib/zod";

const schema = z.object({
  method: z.enum(["written", "email", "e_signature", "verbal_recorded"], { message: "Choose how the consent was given." }),
  may_attest: z.boolean(),
  may_accept_data_processing: z.boolean(),
  note: z.string().trim().max(2000),
  document_reference: z.string().trim().min(1, "Enter where the signed or recorded consent is kept.").max(300),
});
type Values = z.infer<typeof schema>;

export const MAY_ATTEST_HELP =
  "Allows the portal to tick a form's \"my answers are true and correct\" box for the candidate. It never covers a certification that the candidate filled in the form personally.";
export const MAY_ACCEPT_DP_HELP = "Allows the portal to accept the employer's privacy or data-processing notice for the candidate.";

export function RecordConsentDialog({
  candidateId,
  candidateName,
  open,
  onOpenChange,
}: {
  candidateId: string;
  candidateName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const record = useRecordConsent(candidateId);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { method: undefined as unknown as ConsentMethod, may_attest: false, may_accept_data_processing: false, note: "", document_reference: "" },
  });
  const submit = form.handleSubmit((values) =>
    record.mutate(
      { scope: "auto_apply", granted: true, ...values },
      {
        onSuccess: () => {
          toast.success(`Consent recorded for ${candidateName}`);
          form.reset();
          onOpenChange(false);
        },
        onError: (error) => applyServerErrors(form, error),
      },
    ),
  );

  return (
    <Dialog open={open} onOpenChange={(next) => !record.isPending && onOpenChange(next)}>
      <DialogContent className="max-h-[92svh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">Record consent</DialogTitle>
          <DialogDescription>
            {candidateName} agrees that the portal fills in and submits job applications in their name, after an Admin has
            reviewed each one.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <Controller
            control={form.control}
            name="method"
            render={({ field, fieldState }) => (
              <FormField label="How consent was given" error={fieldState.error?.message}>
                {({ id, describedBy, invalid }) => (
                  <Select value={field.value ?? ""} onValueChange={field.onChange}>
                    <SelectTrigger id={id} aria-describedby={describedBy} aria-invalid={invalid || undefined} className="w-full">
                      <SelectValue placeholder="Choose" />
                    </SelectTrigger>
                    <SelectContent>
                      {CONSENT_METHODS.map((m) => (
                        <SelectItem key={m.value} value={m.value}>
                          {m.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </FormField>
            )}
          />
          <TextField
            label="Document reference"
            description="Where the signed form, e-mail or recording is kept."
            error={form.formState.errors.document_reference?.message}
            {...form.register("document_reference")}
          />
          <Controller
            control={form.control}
            name="may_attest"
            render={({ field }) => (
              <label className="flex items-start gap-3 text-sm">
                <Checkbox checked={field.value} onCheckedChange={(v) => field.onChange(v === true)} className="mt-0.5" />
                <span className="flex flex-col gap-0.5">
                  <span className="font-medium">May attest to accuracy</span>
                  <span className="text-2sm text-muted-foreground">{MAY_ATTEST_HELP}</span>
                </span>
              </label>
            )}
          />
          <Controller
            control={form.control}
            name="may_accept_data_processing"
            render={({ field }) => (
              <label className="flex items-start gap-3 text-sm">
                <Checkbox checked={field.value} onCheckedChange={(v) => field.onChange(v === true)} className="mt-0.5" />
                <span className="flex flex-col gap-0.5">
                  <span className="font-medium">May accept privacy notices</span>
                  <span className="text-2sm text-muted-foreground">{MAY_ACCEPT_DP_HELP}</span>
                </span>
              </label>
            )}
          />
          <TextareaField label="Note" optional rows={3} error={form.formState.errors.note?.message} {...form.register("note")} />
          {isGeneralError(form, record.error) && <FormMessage>{errorMessage(record.error)}</FormMessage>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={record.isPending} onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={record.isPending}>
              {record.isPending && <Spinner data-icon="inline-start" />}
              Record consent
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ConsentRow({ consent, onRevoke }: { consent: Consent; onRevoke?: () => void }) {
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 py-3 first:pt-0 last:pb-0">
      <div className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
        <span className="flex flex-wrap items-center gap-2">
          {consent.active ? <Tag tone="success">In force</Tag> : consent.revoked_at ? <Tag>Revoked</Tag> : <Tag>Superseded</Tag>}
          <span className="font-medium">{consentMethodLabel(consent.method)}</span>
          {consent.may_attest && <Tag tone="primary">May attest</Tag>}
          {consent.may_accept_data_processing && <Tag tone="primary">May accept privacy notices</Tag>}
        </span>
        <span className="text-2sm break-words text-muted-foreground">
          Recorded {formatDateTime(consent.recorded_at)}
          {consent.recorded_by ? ` by ${consent.recorded_by.name}` : ""}
          {consent.document_reference ? `. Document: ${consent.document_reference}` : ""}
          {consent.revoked_at ? `. Revoked ${formatDateTime(consent.revoked_at)}${consent.revoked_by ? ` by ${consent.revoked_by.name}` : ""}` : ""}
        </span>
        {consent.note && <span className="text-2sm break-words whitespace-pre-wrap">{consent.note}</span>}
      </div>
      {onRevoke && (
        <Button variant="outline" size="sm" onClick={onRevoke}>
          <ShieldOff data-icon="inline-start" />
          Revoke
        </Button>
      )}
    </li>
  );
}

/** Consent to auto-apply: what is in force, its history, record and revoke. Admin only. */
export function ConsentPanel({ candidateId, candidateName }: { candidateId: string; candidateName: string }) {
  const consents = useConsents(candidateId);
  const revoke = useRevokeConsent(candidateId);
  const [recording, setRecording] = useState(false);
  const [revoking, setRevoking] = useState<Consent | null>(null);

  if (consents.isPending) return <LoadingSkeleton variant="list" count={2} />;
  if (consents.isError) return <ErrorState compact error={consents.error} onRetry={() => consents.refetch()} />;
  const current = activeConsent(consents.data);
  const history = [...consents.data].sort((a, b) => (b.recorded_at ?? "").localeCompare(a.recorded_at ?? ""));

  return (
    <div className="flex flex-col gap-4">
      {!current && (
        <EmptyState
          compact
          icon={FileSignature}
          title="No consent in force"
          description={`Nothing can be submitted for ${candidateName} until their consent is recorded.`}
        />
      )}
      {history.length > 0 && (
        <ul className="flex flex-col divide-y">
          {history.map((c) => (
            <ConsentRow key={c.id} consent={c} onRevoke={c === current ? () => setRevoking(c) : undefined} />
          ))}
        </ul>
      )}
      <div>
        <Button variant={current ? "outline" : "default"} onClick={() => setRecording(true)}>
          <ShieldCheck data-icon="inline-start" />
          {current ? "Record new consent" : "Record consent"}
        </Button>
      </div>
      <RecordConsentDialog candidateId={candidateId} candidateName={candidateName} open={recording} onOpenChange={setRecording} />
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title={`Revoke ${candidateName}'s consent?`}
        description="No application will be submitted in the candidate's name from now on, and every open run of this candidate is cancelled. Applications already submitted stay submitted."
        confirmLabel="Revoke consent"
        destructive
        pending={revoke.isPending}
        onConfirm={() =>
          revoking &&
          revoke.mutate({ consentId: revoking.id }, {
            onSuccess: () => {
              toast.success("Consent revoked");
              setRevoking(null);
            },
            onError: (error) => toast.error(errorMessage(error)),
          })
        }
      />
    </div>
  );
}
