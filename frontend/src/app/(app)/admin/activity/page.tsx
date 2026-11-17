import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AuditLogView } from "@/components/views/audit-log";

export const metadata: Metadata = { title: "Activity Log" };

export default function AdminActivityPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AuditLogView />
    </Suspense>
  );
}
