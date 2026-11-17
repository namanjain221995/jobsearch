import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamLeadAnalyticsView } from "@/components/views/team-analytics";

export const metadata: Metadata = { title: "Analytics" };

export default function TlAnalyticsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamLeadAnalyticsView />
    </Suspense>
  );
}
