"use client";

import { useId } from "react";
import { Controller, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Lock, StickyNote } from "lucide-react";
import { toast } from "sonner";
import { z } from "@/lib/zod";

import { FormMessage, TextareaField } from "@/components/shared/form-fields";
import { Tag } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { applyServerErrors, isGeneralError } from "@/lib/forms";
import { useAddNote } from "@/lib/queries";
import type { AssignmentDetail } from "@/lib/types";

const schema = z.object({
  note: z.string().trim().min(1, "Write the note first.").max(4000, "Use at most 4,000 characters."),
  is_private: z.boolean(),
});
type Values = z.infer<typeof schema>;

/** Notes on one assignment. Managers can keep a note private; members never receive private notes. */
export function AssignmentNotes({ assignment, canWritePrivate }: { assignment: AssignmentDetail; canWritePrivate: boolean }) {
  const privateId = useId();
  const add = useAddNote(assignment.id);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { note: "", is_private: false } });

  const onSubmit = form.handleSubmit((values) => {
    add.mutate(
      { note: values.note, is_private: canWritePrivate ? values.is_private : false },
      {
        onSuccess: () => {
          form.reset();
          toast.success("Note added");
        },
        onError: (error) => {
          applyServerErrors(form, error);
        },
      },
    );
  });
  const failure = isGeneralError(form, add.error);

  return (
    <div className="flex flex-col gap-4">
      {assignment.notes.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <StickyNote className="size-4" aria-hidden="true" />
          No notes yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {assignment.notes.map((note) => (
            <li key={note.id} className="rounded-md border bg-muted/40 px-3 py-2.5">
              <p className="text-sm break-words whitespace-pre-line">{note.note}</p>
              <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                <span>{note.author?.name ?? "Unknown author"}</span>
                <time dateTime={note.created_at}>{formatDateTime(note.created_at)}</time>
                {note.is_private && (
                  <Tag tone="warning">
                    <Lock aria-hidden="true" />
                    Private
                  </Tag>
                )}
              </p>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-3">
        {failure && <FormMessage>{errorMessage(add.error)}</FormMessage>}
        <TextareaField
          label="Add a note"
          rows={2}
          placeholder="Recruiter name, next step, anything worth remembering"
          error={form.formState.errors.note?.message}
          {...form.register("note")}
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          {canWritePrivate ? (
            <div className="flex items-center gap-2">
              <Controller
                control={form.control}
                name="is_private"
                render={({ field }) => (
                  <Checkbox
                    id={privateId}
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                    ref={field.ref}
                  />
                )}
              />
              <Label htmlFor={privateId} className="font-normal">
                Private (hidden from the member)
              </Label>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">Your Team Lead can read your notes.</span>
          )}
          <Button type="submit" variant="outline" size="sm" disabled={add.isPending}>
            {add.isPending && <Spinner data-icon="inline-start" />}
            Add Note
          </Button>
        </div>
      </form>
    </div>
  );
}
