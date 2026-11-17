"use client";

import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { KeyRound } from "lucide-react";
import { z } from "@/lib/zod";

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
          <KeyRound className="size-5" aria-hidden="true" />
        </span>
        <AuthHeading title="Reset your password" />
        {/*
          What happened is told by the API alone, in its own words. Its answer depends on how the server
          delivers mail (sent by e-mail, written to the server log, or not sent at all because the server
          cannot send mail), and it is the same whether or not the address has an account. So nothing on
          this screen says that a message is on its way.
        */}
        <p role="status" className="-mt-4 text-md text-foreground">
          {request.data.message}
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
        Enter the e-mail address of your account to ask for a link to choose a new password.
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
          {request.isPending ? "Sending request" : "Send reset link"}
        </Button>
        <Link href="/login" className="self-start rounded-sm text-sm text-primary underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </form>
    </>
  );
}
