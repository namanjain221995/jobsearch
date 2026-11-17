"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { CircleCheck, Link2Off } from "lucide-react";
import { z } from "@/lib/zod";

import { AuthHeading } from "@/components/auth/auth-heading";
import { PasswordRules } from "@/components/auth/password-rules";
import { FormMessage, PasswordField } from "@/components/shared/form-fields";
import { LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, errorMessage } from "@/lib/api";
import { passwordProblem, type PasswordLimits } from "@/lib/password";
import { usePasswordPolicy, useResetPassword } from "@/lib/queries";
import type { PasswordPolicy } from "@/lib/types";

/** The policy's numbers other than the minimum length, in the shape the password checks take. */
const limitsOf = (policy: PasswordPolicy | undefined): PasswordLimits | undefined =>
  policy && { maxLength: policy.max_length, requiredClasses: policy.character_kinds_required };

/**
 * `policy` is missing only when it could not be loaded. The minimum length is then the server's to
 * state in its answer; here only the character rule is known for sure.
 */
const makeSchema = (policy: PasswordPolicy | undefined) =>
  z.object({ password: z.string(), confirm: z.string() }).superRefine((values, context) => {
    const problem = values.password
      ? passwordProblem(values.password, policy?.min_length ?? 1, limitsOf(policy))
      : "Choose a new password.";
    if (problem) context.addIssue({ code: "custom", path: ["password"], message: problem });
    if (values.confirm !== values.password) {
      context.addIssue({ code: "custom", path: ["confirm"], message: "The two passwords do not match." });
    }
  });
type Values = z.infer<ReturnType<typeof makeSchema>>;

function InvalidLink({ message }: { message: string }) {
  return (
    <div className="flex flex-col gap-5">
      <span className="grid size-10 place-items-center rounded-md border border-border-strong text-muted-foreground">
        <Link2Off className="size-5" aria-hidden="true" />
      </span>
      <AuthHeading title="This link cannot be used" />
      <p role="alert" className="-mt-4 text-md">
        {message}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button asChild>
          <Link href="/forgot-password">Request a new link</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/login">Back to sign in</Link>
        </Button>
      </div>
    </div>
  );
}

function NewPasswordForm({ token, policy }: { token: string; policy: PasswordPolicy | undefined }) {
  const reset = useResetPassword();
  const schema = useMemo(() => makeSchema(policy), [policy]);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { password: "", confirm: "" },
    mode: "onTouched",
  });
  const password = useWatch({ control: form.control, name: "password" });
  const { errors } = form.formState;

  if (reset.isSuccess) {
    return (
      <div className="flex flex-col gap-5">
        <span className="grid size-10 place-items-center rounded-md border border-border-strong text-success">
          <CircleCheck className="size-5" aria-hidden="true" />
        </span>
        <AuthHeading title="Password updated" />
        <p role="status" className="-mt-4 text-md">
          {reset.data.message}
        </p>
        <Button asChild className="self-start">
          <Link href="/login">Sign in</Link>
        </Button>
      </div>
    );
  }

  // 400 means the token itself is no good; 422 is a problem with the chosen password.
  if (reset.error instanceof ApiError && reset.error.status === 400) {
    return <InvalidLink message={reset.error.message} />;
  }

  const onSubmit = form.handleSubmit((values) => {
    reset.mutate(
      { token, password: values.password },
      {
        onError: (error) => {
          if (error instanceof ApiError && error.status === 422) {
            form.setError("password", { type: "server", message: error.fields.password ?? error.message });
            form.setFocus("password");
          }
        },
      },
    );
  });

  const failure = reset.error && !(reset.error instanceof ApiError && reset.error.status === 422);

  return (
    <>
      <AuthHeading title="Choose a new password">
        Signing in with the new password ends every other session of your account.
      </AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
        {failure && <FormMessage>{errorMessage(reset.error)}</FormMessage>}
        <PasswordField
          label="New password"
          autoComplete="new-password"
          autoFocus
          error={errors.password?.message}
          {...form.register("password")}
        />
        <PasswordRules password={password} minLength={policy?.min_length} limits={limitsOf(policy)} />
        <PasswordField
          label="Repeat the new password"
          autoComplete="new-password"
          error={errors.confirm?.message}
          {...form.register("confirm")}
        />
        <Button type="submit" size="lg" disabled={reset.isPending}>
          {reset.isPending && <Spinner data-icon="inline-start" />}
          {reset.isPending ? "Saving password" : "Save new password"}
        </Button>
      </form>
    </>
  );
}

export function ResetPasswordForm() {
  const token = useSearchParams().get("token") ?? "";
  const usable = token.length >= 20;
  const policy = usePasswordPolicy(usable);

  if (!usable) {
    return <InvalidLink message="The reset link is incomplete. Open the link from the e-mail again, or request a new one." />;
  }
  // The rules are fetched before the form is shown, so the list states the minimum length before
  // anything is typed. If they cannot be loaded the form still works: the API checks the password.
  if (policy.isPending) return <LoadingSkeleton variant="form" count={3} />;
  return <NewPasswordForm token={token} policy={policy.data} />;
}
