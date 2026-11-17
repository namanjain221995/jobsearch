"use client";

import Link from "next/link";
import { useId } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "@/lib/zod";

import { AuthHeading } from "@/components/auth/auth-heading";
import { FormMessage, PasswordField, TextField } from "@/components/shared/form-fields";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, safeNextPath } from "@/lib/api";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { useLogin } from "@/lib/queries";
import type { Me } from "@/lib/types";

const schema = z.object({
  email: z.email("Enter a valid e-mail address."),
  password: z.string().min(1, "Enter your password.").max(200, "That password is too long."),
  remember: z.boolean(),
});
type Values = z.infer<typeof schema>;

const AREA_OF_ROLE: Record<Me["role"], string> = { ADMIN: "/admin", TEAM_LEAD: "/tl", MEMBER: "/member" };

/** After signing in: back to where the person was going, if that place belongs to their role. */
function destination(me: Me): string {
  const next = safeNextPath(new URLSearchParams(window.location.search).get("next"));
  const area = AREA_OF_ROLE[me.role];
  if (next && (next === area || next.startsWith(`${area}/`))) return next;
  return me.home;
}

export function LoginForm() {
  const rememberId = useId();
  const login = useLogin();
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "", remember: false },
  });
  const { errors } = form.formState;
  // Keeps the button busy until the browser has left for the dashboard.
  const leaving = login.isSuccess;

  const onSubmit = form.handleSubmit((values) => {
    login.mutate(values, {
      onSuccess: (me) => {
        // A full navigation: the session cookie is new, so every cached screen must be rebuilt.
        window.location.assign(destination(me));
      },
      onError: (error) => {
        applyServerErrors(form, error);
        form.setFocus(error instanceof ApiError && error.fields.email ? "email" : "password");
      },
    });
  });

  const failure = isGeneralError(form, login.error);

  return (
    <>
      <AuthHeading title="Sign in">Use the e-mail address your invitation was sent to.</AuthHeading>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
        {failure && <FormMessage>{login.error?.message}</FormMessage>}
        <TextField
          label="E-mail"
          type="email"
          autoComplete="username"
          inputMode="email"
          autoCapitalize="none"
          spellCheck={false}
          autoFocus
          error={errors.email?.message}
          {...form.register("email")}
        />
        <PasswordField
          label="Password"
          autoComplete="current-password"
          error={errors.password?.message}
          labelAction={
            <Link
              href="/forgot-password"
              className="rounded-sm text-2sm text-primary underline-offset-4 hover:underline"
            >
              Forgot password?
            </Link>
          }
          {...form.register("password")}
        />
        <div className="flex items-center gap-2.5">
          <Controller
            control={form.control}
            name="remember"
            render={({ field }) => (
              <Checkbox
                id={rememberId}
                checked={field.value}
                onCheckedChange={(checked) => field.onChange(checked === true)}
                onBlur={field.onBlur}
                ref={field.ref}
              />
            )}
          />
          <Label htmlFor={rememberId} className="font-normal">
            Keep me signed in on this device
          </Label>
        </div>
        <Button type="submit" size="lg" disabled={login.isPending || leaving} className="mt-1">
          {(login.isPending || leaving) && <Spinner data-icon="inline-start" />}
          {login.isPending || leaving ? "Signing in" : "Sign in"}
        </Button>
      </form>
    </>
  );
}
