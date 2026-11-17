import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamJobsView } from "@/components/views/jobs";

export const metadata: Metadata = { title: "Pending Jobs" };

export default function TlJobsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamJobsView />
    </Suspense>
  );
}
