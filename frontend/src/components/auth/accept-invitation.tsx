"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Link2Off } from "lucide-react";
import { toast } from "sonner";
import { z } from "zod";

import { AuthHeading } from "@/components/auth/auth-heading";
import { PasswordRules } from "@/components/auth/password-rules";
import { FormMessage, PasswordField } from "@/components/shared/form-fields";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, roleLabel } from "@/lib/format";
import { passwordProblem } from "@/lib/password";
import { useAcceptInvitation, useInvitationPreview } from "@/lib/queries";
import type { InvitationPreview } from "@/lib/types";

function InvalidInvitation({ message }: { message: string }) {
  return (
    <div className="flex flex-col gap-5">
      <span className="grid size-10 place-items-center rounded-md border border-border-strong text-muted-foreground">
        <Link2Off className="size-5" aria-hidden="true" />
      </span>
      <AuthHeading title="This invitation cannot be used" />
      <p role="alert" className="-mt-4 text-md">
        {message}
      </p>
      <p className="text-sm text-muted-foreground">
        Ask the person who invited you to send a new invitation. If you already set a password, sign in instead.
      </p>
      <Button asChild className="self-start">
        <Link href="/login">Go to sign in</Link>
      </Button>
    </div>
  );
}

function AcceptForm({ token, invitation }: { token: string; invitation: InvitationPreview }) {
  const router = useRouter();
  const accept = useAcceptInvitation();
  const minLength = invitation.password_min_length;

  const schema = z
    .object({ password: z.string(), confirm: z.string() })
    .superRefine((values, context) => {
      const problem = passwordProblem(values.password, minLength);
      if (problem) context.addIssue({ code: "custom", path: ["password"], message: problem });
      if (values.confirm !== values.password) {
        context.addIssue({ code: "custom", path: ["confirm"], message: "The two passwords do not match." });
      }
    });
  type Values = z.infer<typeof schema>;

  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { password: "", confirm: "" },
    mode: "onTouched",
  });
  const password = useWatch({ control: form.control, name: "password" });
  const { errors } = form.formState;

  if (accept.error instanceof ApiError && accept.error.status === 400) {
    return <InvalidInvitation message={accept.error.message} />;
  }

  const onSubmit = form.handleSubmit((values) => {
    accept.mutate(
      { token, password: values.password },
      {
        onSuccess: (result) => {
          toast.success(result.message);
          router.push("/login");
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 422) {
            form.setError("password", { type: "server", message: error.fields.password ?? error.message });
            form.setFocus("password");
          }
        },
      },
    );
  });

  const failure = accept.error && !(accept.error instanceof ApiError && accept.error.status === 422);
  const busy = accept.isPending || accept.isSuccess;

  return (
    <>
      <AuthHeading title={`Welcome, ${invitation.name}`}>
        Choose a password to finish setting up your account.
      </AuthHeading>

      <dl className="mb-6 grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 rounded-md border bg-card px-4 py-3 text-sm">
        <dt className="text-muted-foreground">E-mail</dt>
        <dd className="min-w-0 break-words font-medium">{invitation.email}</dd>
        <dt className="text-muted-foreground">Team</dt>
        <dd className="font-medium">{invitation.team_name ?? "No team yet"}</dd>
        <dt className="text-muted-foreground">Role</dt>
        <dd className="font-medium">{roleLabel(invitation.role)}</dd>
        <dt className="text-muted-foreground">Link valid until</dt>
        <dd>{formatDateTime(invitation.expires_at)}</dd>
      </dl>

      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
        {failure && <FormMessage>{errorMessage(accept.error)}</FormMessage>}
        {/* Lets password managers file the new password under the right account. */}
        <input type="text" name="username" autoComplete="username" value={invitation.email} readOnly hidden />
        <PasswordField
          label="Password"
          autoComplete="new-password"
          autoFocus
          error={errors.password?.message}
          {...form.register("password")}
        />
        <PasswordRules password={password} minLength={minLength} />
        <PasswordField
          label="Repeat the password"
          autoComplete="new-password"
          error={errors.confirm?.message}
          {...form.register("confirm")}
        />
        <Button type="submit" size="lg" disabled={busy}>
          {busy && <Spinner data-icon="inline-start" />}
          {busy ? "Creating account" : "Create account"}
        </Button>
      </form>
    </>
  );
}

export function AcceptInvitation() {
  const token = useSearchParams().get("token") ?? "";
  const preview = useInvitationPreview(token);

  if (token.length < 20) {
    return (
      <InvalidInvitation message="The invitation link is incomplete. Open the link from your invitation again." />
    );
  }
  if (preview.isPending) return <LoadingSkeleton variant="form" count={4} />;
  if (preview.isError) {
    const error = preview.error;
    // 404 (unknown, used, expired or revoked) and 422 (malformed token) both mean: this link is no good.
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) {
      return <InvalidInvitation message={error.status === 404 ? error.message : "This invitation link is invalid or has expired."} />;
    }
    return <ErrorState error={error} onRetry={() => preview.refetch()} />;
  }
  return <AcceptForm token={token} invitation={preview.data} />;
}
