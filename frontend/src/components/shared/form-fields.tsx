"use client";

import { useId, useState, type ComponentProps, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";

import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface FieldShellProps {
  label: ReactNode;
  /** Shown under the control while there is no error. */
  description?: ReactNode;
  error?: string;
  /** Marks the label for fields that may be left empty. */
  optional?: boolean;
  className?: string;
}

function describedBy(...ids: Array<string | false | undefined>) {
  const list = ids.filter(Boolean).join(" ");
  return list || undefined;
}

export function OptionalMark() {
  return <span className="font-normal text-muted-foreground">(optional)</span>;
}

/** Label, control, help text and error wired together for any control. */
export function FormField({
  label,
  description,
  error,
  optional,
  className,
  children,
}: FieldShellProps & {
  children: (ids: { id: string; describedBy: string | undefined; invalid: boolean }) => ReactNode;
}) {
  const id = useId();
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  return (
    <Field data-invalid={error ? true : undefined} className={className}>
      <FieldLabel htmlFor={id}>
        {label}
        {optional && <OptionalMark />}
      </FieldLabel>
      {children({ id, describedBy: describedBy(description ? descriptionId : undefined, error ? errorId : undefined), invalid: Boolean(error) })}
      {description && !error && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
      {error && <FieldError id={errorId}>{error}</FieldError>}
    </Field>
  );
}

export function TextField({
  label,
  description,
  error,
  optional,
  className,
  ...props
}: FieldShellProps & Omit<ComponentProps<typeof Input>, "id" | "className">) {
  return (
    <FormField label={label} description={description} error={error} optional={optional} className={className}>
      {({ id, describedBy, invalid }) => (
        <Input id={id} aria-invalid={invalid || undefined} aria-describedby={describedBy} {...props} />
      )}
    </FormField>
  );
}

export function TextareaField({
  label,
  description,
  error,
  optional,
  className,
  ...props
}: FieldShellProps & Omit<ComponentProps<typeof Textarea>, "id" | "className">) {
  return (
    <FormField label={label} description={description} error={error} optional={optional} className={className}>
      {({ id, describedBy, invalid }) => (
        <Textarea id={id} aria-invalid={invalid || undefined} aria-describedby={describedBy} {...props} />
      )}
    </FormField>
  );
}

export function PasswordField({
  label,
  description,
  error,
  className,
  labelAction,
  ...props
}: FieldShellProps & { labelAction?: ReactNode } & Omit<ComponentProps<"input">, "id" | "className" | "type">) {
  const id = useId();
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  const [visible, setVisible] = useState(false);
  return (
    <Field data-invalid={error ? true : undefined} className={className}>
      <div className="flex items-center justify-between gap-3">
        <FieldLabel htmlFor={id}>{label}</FieldLabel>
        {labelAction}
      </div>
      <InputGroup>
        <InputGroupInput
          id={id}
          type={visible ? "text" : "password"}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy(description ? descriptionId : undefined, error ? errorId : undefined)}
          {...props}
        />
        <InputGroupAddon align="inline-end">
          <InputGroupButton
            size="icon-xs"
            aria-label={visible ? "Hide password" : "Show password"}
            aria-pressed={visible}
            onClick={() => setVisible((value) => !value)}
          >
            {visible ? <EyeOff /> : <Eye />}
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
      {description && !error && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
      {error && <FieldError id={errorId}>{error}</FieldError>}
    </Field>
  );
}

/** A form-level message (sign-in failed, save failed). Announced to screen readers. */
export function FormMessage({
  tone = "error",
  children,
  className,
}: {
  tone?: "error" | "success" | "info";
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "rounded-md border px-3 py-2 text-sm",
        tone === "error" && "border-destructive/35 bg-destructive/5 text-destructive",
        tone === "success" && "border-success/35 bg-success/5 text-success",
        tone === "info" && "border-border-strong bg-muted text-foreground",
        className,
      )}
    >
      {children}
    </div>
  );
}
