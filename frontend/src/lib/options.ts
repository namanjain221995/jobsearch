/**
 * The fixed choices the API accepts for profile and search fields. Each list is
 * checked against the generated contract types, so a value the API does not know
 * cannot be offered and a new one cannot be forgotten without a type error.
 */
import type { JobCreate, ProfileIn } from "./types";

type Authorization = NonNullable<ProfileIn["work_authorization"]>;
type WorkMode = NonNullable<ProfileIn["work_mode"]>;
type Sponsorship = NonNullable<ProfileIn["sponsorship_needed"]>;
type JobWorkMode = JobCreate["work_mode"];
type Citizenship = JobCreate["citizenship_requirement"];
type SponsorshipStatus = JobCreate["sponsorship_status"];

/** Fails to compile unless `T` lists every member of the union `U`. */
type Exhaustive<U, T extends readonly U[]> = [U] extends [T[number]] ? T : never;
const all = <U>() => <const T extends readonly U[]>(values: Exhaustive<U, T>) => values;

export const AUTHORIZATIONS = all<Authorization>()([
  "US Citizen",
  "Green Card / US Permanent Resident",
  "Current Work Visa",
  "Employment Authorization Document (EAD)",
  "Require Visa Sponsorship",
  "Other / Prefer Not to Say",
]);

export const WORK_MODES = all<WorkMode>()(["All", "Remote", "Hybrid", "On-site"]);
export const WORK_MODE_LABELS: Record<WorkMode, string> = {
  All: "Any arrangement",
  Remote: "Remote",
  Hybrid: "Hybrid",
  "On-site": "On-site",
};

export const SPONSORSHIP = all<Sponsorship>()(["Yes", "No", "Unknown"]);
export const SPONSORSHIP_LABELS: Record<Sponsorship, string> = {
  Yes: "Yes, sponsorship is needed",
  No: "No sponsorship needed",
  Unknown: "Not sure",
};

export const JOB_WORK_MODES = all<JobWorkMode>()(["Remote", "Hybrid", "On-site", "Not listed"]);
export const CITIZENSHIP_REQUIREMENTS = all<Citizenship>()(["Required", "Not required", "Not stated"]);
export const SPONSORSHIP_STATUSES = all<SponsorshipStatus>()(["Available", "Not available", "Not stated"]);

export const US_STATES: Array<[code: string, name: string]> = [
  ["AL", "Alabama"], ["AK", "Alaska"], ["AZ", "Arizona"], ["AR", "Arkansas"], ["CA", "California"],
  ["CO", "Colorado"], ["CT", "Connecticut"], ["DE", "Delaware"], ["DC", "District of Columbia"], ["FL", "Florida"],
  ["GA", "Georgia"], ["HI", "Hawaii"], ["ID", "Idaho"], ["IL", "Illinois"], ["IN", "Indiana"],
  ["IA", "Iowa"], ["KS", "Kansas"], ["KY", "Kentucky"], ["LA", "Louisiana"], ["ME", "Maine"],
  ["MD", "Maryland"], ["MA", "Massachusetts"], ["MI", "Michigan"], ["MN", "Minnesota"], ["MS", "Mississippi"],
  ["MO", "Missouri"], ["MT", "Montana"], ["NE", "Nebraska"], ["NV", "Nevada"], ["NH", "New Hampshire"],
  ["NJ", "New Jersey"], ["NM", "New Mexico"], ["NY", "New York"], ["NC", "North Carolina"], ["ND", "North Dakota"],
  ["OH", "Ohio"], ["OK", "Oklahoma"], ["OR", "Oregon"], ["PA", "Pennsylvania"], ["RI", "Rhode Island"],
  ["SC", "South Carolina"], ["SD", "South Dakota"], ["TN", "Tennessee"], ["TX", "Texas"], ["UT", "Utah"],
  ["VT", "Vermont"], ["VA", "Virginia"], ["WA", "Washington"], ["WV", "West Virginia"], ["WI", "Wisconsin"],
  ["WY", "Wyoming"],
];

const STATE_BY_NAME = new Map(US_STATES.map(([code, name]) => [name.toLowerCase(), code]));
const STATE_CODES = new Set(US_STATES.map(([code]) => code));

/** "new york" and "ny" both become "NY". Anything that is not a US state is kept as typed. */
export function normalizeState(value: string): string {
  const text = value.trim();
  const upper = text.toUpperCase();
  if (STATE_CODES.has(upper)) return upper;
  return STATE_BY_NAME.get(text.toLowerCase()) ?? text;
}
