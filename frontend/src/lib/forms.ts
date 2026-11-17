import type { FieldValues, Path, UseFormReturn } from "react-hook-form";
import { z } from "@/lib/zod";

import { ApiError } from "./api";
import { EXTRA_FIELD_TYPES } from "./options";
import type { ExtraField } from "./types";

/**
 * A person's name as the API accepts it (PersonName in the backend schema): 2 to
 * 100 characters that start with a letter or a digit and contain only letters,
 * digits, spaces and . , ' - ( ). Names are printed in the e-mails the portal
 * sends, so they cannot carry a link or a line break. The API checks again and
 * answers 422 with `fields.name`; this only says it sooner.
 */
export const PERSON_NAME_RULE = "Use 2 to 100 characters, starting with a letter or a digit: letters, digits, spaces and . , ' - ( ) only.";
const PERSON_NAME = /^[\p{L}\p{N}][\p{L}\p{N}_ .,'\u2019()-]{1,99}$/u;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

export function isPersonName(value: string): boolean {
  if (CONTROL_CHARACTER.test(value)) return false;
  return PERSON_NAME.test(value.trim().replace(/\s+/g, " "));
}

export const personNameSchema = z.string().trim().refine(isPersonName, PERSON_NAME_RULE);

/**
 * Put the API's per-field validation messages (`fields` in the error body) on the
 * matching form fields. Returns true when at least one message found its field.
 */
export function applyServerErrors<T extends FieldValues>(form: UseFormReturn<T>, error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  const known = new Set(Object.keys(form.getValues()));
  let applied = false;
  for (const [name, message] of Object.entries(error.fields)) {
    const root = name.split(".")[0];
    if (!known.has(root)) continue;
    form.setError(name as Path<T>, { type: "server", message });
    applied = true;
  }
  return applied;
}

/**
 * Whether a failed request has to be shown as a message for the whole form:
 * the API named no field, or only fields this form does not have. Without this
 * a refusal about something the form does not show would be shown nowhere.
 */
export function isGeneralError<T extends FieldValues>(form: UseFormReturn<T>, error: unknown): boolean {
  if (!error) return false;
  if (!(error instanceof ApiError)) return true;
  const known = new Set(Object.keys(form.getValues()));
  return !Object.keys(error.fields).some((name) => known.has(name.split(".")[0]));
}

/** Turn "a, b\nc" into ["a", "b", "c"]: for the comma separated inputs (titles, states, companies). */
export function splitList(value: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of value.split(/[,\n]/)) {
    const item = part.trim();
    const key = item.toLowerCase();
    if (!item || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

/* ------------------------------------------------------------------ extra candidate-profile fields: what an Admin defines */

export const EXTRA_FIELDS_MAX = 20;
export const EXTRA_OPTIONS_MIN = 2;
export const EXTRA_OPTIONS_MAX = 20;
const EXTRA_NAME_MAX = 60;
const EXTRA_FIELD_KEY = /^[a-z][a-z0-9_]{0,39}$/;

/** Length as the API counts it: in characters. JavaScript's `length` would count an emoji as two. */
const characters = (value: string) => Array.from(value).length;

const shortText = (message: string) =>
  z.string().refine((value) => value.length > 0 && characters(value) <= EXTRA_NAME_MAX, message);

/**
 * The extra profile fields as PATCH /api/settings accepts them (ExtraField and SettingsUpdate in
 * the backend schema), rule for rule: at most 20 fields; a key that is a lower-case letter followed
 * by up to 39 lower-case letters, digits or underscores, and that no other field has; a label of 1
 * to 60 characters; options of 1 to 60 characters each, at most 20 of them, and at least 2 for a
 * choice. The API checks again and answers 422; this only says it sooner.
 */
export const extraFieldSchema = z
  .object({
    key: z
      .string()
      .regex(EXTRA_FIELD_KEY, "A key is a lower-case letter followed by up to 39 lower-case letters, digits or underscores."),
    label: shortText("Give the field a name of 1 to 60 characters."),
    type: z.enum(EXTRA_FIELD_TYPES),
    options: z
      .array(shortText("Each option needs 1 to 60 characters."))
      .max(EXTRA_OPTIONS_MAX, `A choice can have at most ${EXTRA_OPTIONS_MAX} options.`),
  })
  .superRefine((field, context) => {
    if (field.type === "choice" && field.options.length < EXTRA_OPTIONS_MIN) {
      context.addIssue({ code: "custom", path: ["options"], message: "A choice needs at least two options." });
    }
  });

export const extraFieldsSchema = z
  .array(extraFieldSchema)
  .max(EXTRA_FIELDS_MAX, `There can be at most ${EXTRA_FIELDS_MAX} extra fields.`)
  .superRefine((fields, context) => {
    const seen = new Set<string>();
    fields.forEach((field, index) => {
      if (seen.has(field.key)) {
        context.addIssue({ code: "custom", path: [index, "key"], message: "Another field already has this key. Each field needs its own." });
      }
      seen.add(field.key);
    });
  });

/**
 * The key of a new field, made from its label: lower-case, with every run of other characters
 * turned into one underscore ("Shirt size" becomes "shirt_size"). A key starts with a letter and
 * has at most 40 characters, and no two fields share one, so a number is added to a key that is
 * taken ("shirt_size_2").
 *
 * Only for a field that has not been saved yet. Answers are stored under the key, so a field that
 * exists keeps its key whatever happens to its label.
 */
export function extraFieldKey(label: string, taken: ReadonlySet<string>): string {
  let base = label
    .normalize("NFKD")
    .replace(/\p{M}/gu, "") // NFKD split an accented letter into the letter and its accent: drop the accent
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!/^[a-z]/.test(base)) base = base ? `field_${base}` : "field";
  base = base.slice(0, 40).replace(/_+$/, "");
  let key = base;
  for (let n = 2; taken.has(key); n += 1) {
    const suffix = `_${n}`;
    key = base.slice(0, 40 - suffix.length).replace(/_+$/, "") + suffix;
  }
  return key;
}

/* ------------------------------------------------------------------ extra candidate-profile fields: what is answered for a candidate */

/** The longest text answer the API keeps (it cuts off the rest). */
export const EXTRA_ANSWER_MAX = 500;
/** The API refuses a number beyond this, in either direction. */
const EXTRA_NUMBER_LIMIT = 1e12;
/** Digits with an optional sign, decimal point and exponent: what both JavaScript and the API read as a number. */
const DECIMAL_NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;
/** How a yes/no answer travels inside a form, where every answer is text. */
export const EXTRA_YES = "yes";
export const EXTRA_NO = "no";

/** What the API stores for an answer: text, a number, or true / false. */
export type ExtraAnswer = string | number | boolean;

const collapseSpaces = (text: string) => text.trim().replace(/\s+/g, " ");

/**
 * What is wrong with an answer as it stands in the form, or null. No answer is never wrong: every
 * extra field is optional. The API checks the same (a number, yes or no, one of the options) and
 * answers 422 with a sentence of its own.
 */
export function extraAnswerProblem(field: ExtraField, text: string): string | null {
  // An option is compared exactly as the Admin wrote it.
  if (field.type === "choice") {
    return text === "" || (field.options ?? []).includes(text) ? null : "Choose one of the listed options.";
  }
  const answer = text.trim();
  if (answer === "") return null;
  if (field.type === "number") {
    if (!DECIMAL_NUMBER.test(answer)) return "Enter a number, such as 3 or 2.5.";
    return Math.abs(Number(answer)) > EXTRA_NUMBER_LIMIT ? "Enter a number no larger than 1,000,000,000,000." : null;
  }
  if (field.type === "yes_no") return answer === EXTRA_YES || answer === EXTRA_NO ? null : "Choose yes or no.";
  return characters(collapseSpaces(text)) > EXTRA_ANSWER_MAX ? `Use at most ${EXTRA_ANSWER_MAX} characters.` : null;
}

/** An answer from the form as the API takes it. Null stands for no answer; sent to the API, it clears a stored one. */
export function extraAnswerValue(field: ExtraField, text: string): ExtraAnswer | null {
  if (field.type === "choice") return text === "" ? null : text;
  const answer = text.trim();
  if (answer === "") return null;
  if (field.type === "number") return Number(answer);
  if (field.type === "yes_no") return answer === EXTRA_YES;
  return collapseSpaces(text);
}

/**
 * A stored answer as the form holds it: text, "" for none. An answer that does not fit its field
 * any more (an Admin changed the field's type or options after it was given) counts as none: the
 * control could not show it, and the API would refuse it if it were sent back.
 */
export function extraAnswerText(field: ExtraField, stored: unknown): string {
  switch (field.type) {
    case "yes_no":
      return stored === true ? EXTRA_YES : stored === false ? EXTRA_NO : "";
    case "choice":
      return typeof stored === "string" && (field.options ?? []).includes(stored) ? stored : "";
    case "number": {
      const text = typeof stored === "number" ? String(stored) : typeof stored === "string" ? stored.trim() : "";
      return text !== "" && extraAnswerProblem(field, text) === null ? text : "";
    }
    default:
      return typeof stored === "string" ? stored : typeof stored === "number" ? String(stored) : "";
  }
}
