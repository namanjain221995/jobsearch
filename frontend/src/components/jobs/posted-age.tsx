"use client";

import { useNow } from "@/hooks/use-now";
import { formatPosted } from "@/lib/format";

/**
 * How long ago a job was posted: "Posted 3 days ago", or "Posting date not stated" when the posting gives no date.
 * The age is counted in the viewer's calendar, so it is only worked out in the browser.
 */
export function PostedAge({ value, detailed = false }: { value: string | null | undefined; detailed?: boolean }) {
  const now = useNow();
  return <>{formatPosted(value, now, detailed)}</>;
}
