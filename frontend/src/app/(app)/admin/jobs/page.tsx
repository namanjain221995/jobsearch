import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AdminJobsView } from "@/components/views/jobs";

export const metadata: Metadata = { title: "Jobs" };

export default function AdminJobsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AdminJobsView />
    </Suspense>
  );
}
