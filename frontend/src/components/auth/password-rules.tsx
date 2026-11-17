import { Check, Circle } from "lucide-react";

import { checkPassword, REQUIRED_CLASSES, type PasswordLimits } from "@/lib/password";
import { cn } from "@/lib/utils";

function Rule({ met, children }: { met: boolean; children: React.ReactNode }) {
  return (
    <li className={cn("flex items-start gap-2", met ? "text-foreground" : "text-muted-foreground")}>
      {met ? (
        <Check className="mt-0.5 size-3.5 shrink-0 text-success" aria-hidden="true" />
      ) : (
        <Circle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      )}
      <span>
        {children}
        <span className="sr-only">{met ? " (met)" : " (not met yet)"}</span>
      </span>
    </li>
  );
}

/**
 * Live checklist of the password rules. `minLength` comes from the API on every screen that asks
 * for a new password: the invitation preview, the password policy (reset) and the signed-in
 * user's settings (change). If it could not be loaded, only the character rule is listed and the
 * API's own answer covers the rest. `limits` are the other numbers of the password policy, on the
 * screen that has them.
 */
export function PasswordRules({
  password,
  minLength,
  limits,
  id,
}: {
  password: string;
  minLength?: number;
  limits?: PasswordLimits;
  id?: string;
}) {
  const check = checkPassword(password, minLength ?? 0, limits);
  const requiredKinds = limits?.requiredClasses ?? REQUIRED_CLASSES;
  const kinds: Array<[keyof typeof check.classes, string]> = [
    ["lower", "lower-case letter"],
    ["upper", "upper-case letter"],
    ["digit", "digit"],
    ["symbol", "symbol"],
  ];
  return (
    <div id={id} className="rounded-md border bg-muted/50 px-3 py-2.5 text-2sm">
      <p className="mb-1.5 font-label text-foreground">Your password needs</p>
      <ul className="flex flex-col gap-1">
        {minLength !== undefined && <Rule met={check.lengthOk}>At least {minLength} characters</Rule>}
        <Rule met={check.classesOk}>
          At least {requiredKinds} of these {kinds.length} kinds of character ({check.classCount} so far)
        </Rule>
      </ul>
      <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 pl-5.5 text-muted-foreground">
        {kinds.map(([key, label]) => (
          <li key={key} className={cn("flex items-center gap-1", check.classes[key] && "text-foreground")}>
            {check.classes[key] ? (
              <Check className="size-3 text-success" aria-hidden="true" />
            ) : (
              <Circle className="size-3" aria-hidden="true" />
            )}
            {label}
          </li>
        ))}
      </ul>
    </div>
  );
}
