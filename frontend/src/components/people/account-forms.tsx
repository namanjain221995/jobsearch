"use client";

import { useMemo } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { PasswordRules } from "@/components/auth/password-rules";
import { FormMessage, PasswordField, TextField } from "@/components/shared/form-fields";
import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, errorMessage } from "@/lib/api";
import { roleLabel } from "@/lib/format";
import { applyServerErrors, personNameSchema } from "@/lib/forms";
import { passwordProblem } from "@/lib/password";
import { useChangePassword, useUpdateUser } from "@/lib/queries";

/** `minLength` is the API's own minimum, from the signed-in user's settings (`me.settings.password_min_length`). */
const makePasswordSchema = (minLength: number) =>
  z
    .object({ current_password: z.string().min(1, "Enter your current password."), new_password: z.string(), confirm: z.string() })
    .superRefine((values, context) => {
      const problem = values.new_password ? passwordProblem(values.new_password, minLength) : "Choose a new password.";
      if (problem) context.addIssue({ code: "custom", path: ["new_password"], message: problem });
      if (values.new_password && values.new_password === values.current_password) {
        context.addIssue({ code: "custom", path: ["new_password"], message: "Choose a password you are not using now." });
      }
      if (values.confirm !== values.new_password) {
        context.addIssue({ code: "custom", path: ["confirm"], message: "The two passwords do not match." });
      }
    });
type PasswordValues = z.infer<ReturnType<typeof makePasswordSchema>>;

/** Change the signed-in person's own password. Other devices are signed out by the API when it succeeds. */
export function ChangePasswordForm() {
  const me = useMe();
  const change = useChangePassword();
  const minLength = me.settings.password_min_length;
  const schema = useMemo(() => makePasswordSchema(minLength), [minLength]);
  const form = useForm<PasswordValues>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: "", new_password: "", confirm: "" },
    mode: "onTouched",
  });
  const typed = useWatch({ control: form.control, name: "new_password" });
  const { errors } = form.formState;

  const onSubmit = form.handleSubmit((values) => {
    change.mutate(
      { current_password: values.current_password, new_password: values.new_password },
      {
        onSuccess: (result) => {
          form.reset();
          toast.success(result.message, { description: "Your other devices were signed out." });
        },
        onError: (error) => {
          if (!(error instanceof ApiError)) return;
          if (error.status === 403) {
            form.setError("current_password", { type: "server", message: error.message });
            form.setFocus("current_password");
          } else if (error.status === 422 && !applyServerErrors(form, error)) {
            form.setError("new_password", { type: "server", message: error.message });
            form.setFocus("new_password");
          }
        },
      },
    );
  });
  const handled = change.error instanceof ApiError && (change.error.status === 403 || change.error.status === 422);

  return (
    <form onSubmit={onSubmit} noValidate className="flex max-w-md flex-col gap-5">
      {change.error && !handled && <FormMessage>{errorMessage(change.error)}</FormMessage>}
      <input type="text" name="username" autoComplete="username" value={me.email} readOnly hidden />
      <PasswordField
        label="Current password"
        autoComplete="current-password"
        error={errors.current_password?.message}
        {...form.register("current_password")}
      />
      <PasswordField
        label="New password"
        autoComplete="new-password"
        error={errors.new_password?.message}
        {...form.register("new_password")}
      />
      <PasswordRules password={typed} minLength={minLength} />
      <PasswordField
        label="Repeat the new password"
        autoComplete="new-password"
        error={errors.confirm?.message}
        {...form.register("confirm")}
      />
      <Button type="submit" className="self-start" disabled={change.isPending}>
        {change.isPending && <Spinner data-icon="inline-start" />}
        Change Password
      </Button>
    </form>
  );
}

const nameSchema = z.object({ name: personNameSchema });
type NameValues = z.infer<typeof nameSchema>;

/** The signed-in person's own account: their name can be changed, the rest is set by an Admin. */
export function AccountForm() {
  const me = useMe();
  const update = useUpdateUser();
  const form = useForm<NameValues>({ resolver: zodResolver(nameSchema), defaultValues: { name: me.name } });

  const onSubmit = form.handleSubmit((values) => {
    update.mutate(
      { id: me.id, body: { name: values.name } },
      {
        onSuccess: (user) => {
          form.reset({ name: user.name });
          toast.success("Name saved");
        },
        onError: (error) => {
          if (!applyServerErrors(form, error)) toast.error(errorMessage(error));
        },
      },
    );
  });

  return (
    <form onSubmit={onSubmit} noValidate className="flex max-w-md flex-col gap-5">
      <TextField label="Name" autoComplete="name" error={form.formState.errors.name?.message} {...form.register("name")} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-sm">
        <dt className="text-muted-foreground">E-mail</dt>
        <dd className="break-words">{me.email}</dd>
        <dt className="text-muted-foreground">Role</dt>
        <dd>{roleLabel(me.role)}</dd>
        <dt className="text-muted-foreground">Team</dt>
        <dd>{me.team?.name ?? (me.role === "ADMIN" ? "All teams" : "No team yet")}</dd>
      </dl>
      <Button type="submit" variant="outline" className="self-start" disabled={update.isPending || !form.formState.isDirty}>
        {update.isPending && <Spinner data-icon="inline-start" />}
        Save Name
      </Button>
    </form>
  );
}
