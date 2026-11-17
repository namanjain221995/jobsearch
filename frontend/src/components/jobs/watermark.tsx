"use client";

import type { ReactNode } from "react";

import { useMe } from "@/components/shell/me-context";
import { useNow } from "@/hooks/use-now";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const escapeXml = (value: string) =>
  value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[char] ?? char);

function stamp(now: number): string {
  const date = new Date(now);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * A faint, repeated mark of who is looking at a job and when, laid over member
 * job views when the portal setting is on. It identifies the source of a copy;
 * it does not and cannot stop a screenshot. The layer is decorative to assistive
 * technology and never receives pointer or keyboard input.
 */
function WatermarkLayer({ email }: { email: string }) {
  const now = useNow();
  const { resolvedTheme } = useTheme();
  if (now === null) return null;

  const ink = resolvedTheme === "dark" ? "rgba(232,235,241,0.06)" : "rgba(19,24,37,0.065)";
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="340" height="190">` +
    `<g transform="rotate(-22 170 95)" font-family="system-ui,sans-serif" font-size="13" fill="${ink}">` +
    `<text x="24" y="92">${escapeXml(email)}</text>` +
    `<text x="24" y="111">${escapeXml(stamp(now))}</text>` +
    `</g></svg>`;

  return (
    <div
      aria-hidden="true"
      data-slot="watermark"
      className="pointer-events-none absolute inset-0 z-10 select-none"
      style={{ backgroundImage: `url("data:image/svg+xml,${encodeURIComponent(svg)}")`, backgroundRepeat: "repeat" }}
    />
  );
}

/** Wraps a member's job view. Adds the watermark only for members, and only while the setting is on. */
export function Watermarked({ children, className }: { children: ReactNode; className?: string }) {
  const me = useMe();
  const enabled = me.role === "MEMBER" && me.settings.watermark_enabled;
  return (
    <div className={cn("relative", className)}>
      {children}
      {enabled && <WatermarkLayer email={me.email} />}
    </div>
  );
}
