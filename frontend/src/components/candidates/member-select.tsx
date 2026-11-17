"use client";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useUsers } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Radix Select cannot hold an empty value, so "nobody" travels as this token. */
const NOBODY = "__nobody__";

/**
 * Chooses the member responsible for a candidate: one of the active members of the
 * candidate's own team (the API accepts nobody else), or nobody.
 * The value is the member's id, or "" for unassigned.
 */
export function MemberSelect({
  teamId,
  value,
  onChange,
  current,
  id,
  invalid,
  describedBy,
  disabled,
  label,
  className,
}: {
  /** The candidate's team. Without one there is nobody to choose from yet. */
  teamId: string | undefined;
  value: string;
  onChange: (memberId: string) => void;
  /** The member who is responsible now. Listed even when the member list has not loaded (or holds only the first 200). */
  current?: { id: string; name: string } | null;
  id?: string;
  invalid?: boolean;
  describedBy?: string;
  disabled?: boolean;
  /** Accessible name when no visible label points at `id`. */
  label?: string;
  className?: string;
}) {
  const members = useUsers(
    { role: "MEMBER", team_id: teamId, status: "active", sort: "name", page: 1, page_size: 200 },
    { enabled: Boolean(teamId), refetchInterval: false },
  );
  const items = members.data?.items ?? [];
  const options = current && !items.some((member) => member.id === current.id) ? [current, ...items] : items;
  const placeholder = !teamId ? "Choose a team first" : members.isPending ? "Loading members" : "Choose a member";

  return (
    <Select
      value={value || NOBODY}
      onValueChange={(next) => onChange(next === NOBODY ? "" : next)}
      disabled={disabled || !teamId}
    >
      <SelectTrigger
        id={id}
        aria-label={label}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className={cn("w-full", className)}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={NOBODY}>Nobody yet (unassigned)</SelectItem>
        {options.map((member) => (
          <SelectItem key={member.id} value={member.id}>
            {member.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
