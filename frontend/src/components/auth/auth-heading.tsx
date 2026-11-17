import type { ReactNode } from "react";

export function AuthHeading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-7 flex flex-col gap-2">
      <h1 className="font-display text-3xl leading-tight">{title}</h1>
      {children && <p className="text-md text-muted-foreground">{children}</p>}
    </div>
  );
}
