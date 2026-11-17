"use client";

import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { MailCheck } from "lucide-react";
import { z } from "zod";

import { AuthHeading } from "@/components/auth/auth-heading";
import { FormMessage, TextField } from "@/components/shared/form-fields";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { applyServerErrors } from "@/lib/forms";
import { useForgotPassword } from "@/lib/queries";

const schema = z.object({ email: z.email("Enter a valid e-mail address.") });
type Values = z.infer<typeof schema>;

export function ForgotPasswordForm() {
  const request = useForgotPassword();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "" } });

  const onSubmit = form.handleSubmit((values) => {
    request.mutate(values, { onError: (error) => applyServerErrors(form, error) });
  });

  if (request.isSuccess) {
    return (
      <div className="flex flex-col gap-5">
        <span className="grid size-10 place-items-center rounded-md border border-border-strong text-primary">
          <MailCheck className="size-5" aria-hidden="true" />
        </span>
        <AuthHeading title="Check your inbox" />
        {/* The API answers with the same sentence whether or not the address has an account. */}
        <p role="status" className="-mt-4 text-md text-foreground">
          {request.data.message}
        </p>
        <p className="text-sm text-muted-foreground">
          No message after a few minutes? Check the spelling, look in spam, or ask your Team Lead for a reset
          link.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button asChild>
            <Link href="/login">Back to sign in</Link>
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              request.reset();
              form.reset();
            }}
          >
            Use another address
          </Button>
        </div>
      </div>
    );
  }

  const failure = request.error && !form.formState.errors.email;

  return (
    <>
      <AuthHeading title="Reset your password">
        Enter your e-mail address and we will send a link to choose a new password.
      </AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
        {failure && <FormMessage>{errorMessage(request.error)}</FormMessage>}
        <TextField
          label="E-mail"
          type="email"
          autoComplete="username"
          inputMode="email"
          autoCapitalize="none"
          spellCheck={false}
          autoFocus
          error={form.formState.errors.email?.message}
          {...form.register("email")}
        />
        <Button type="submit" size="lg" disabled={request.isPending}>
          {request.isPending && <Spinner data-icon="inline-start" />}
          {request.isPending ? "Sending link" : "Send reset link"}
        </Button>
        <Link href="/login" className="self-start rounded-sm text-sm text-primary underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </form>
    </>
  );
}
