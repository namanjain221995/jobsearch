import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamLeadDashboardView } from "@/components/views/team-analytics";

export const metadata: Metadata = { title: "Dashboard" };

export default function TlDashboardPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamLeadDashboardView />
    </Suspense>
  );
}
