import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { JobManagerDetailView } from "@/components/views/job-manager-detail";

export const metadata: Metadata = { title: "Job" };

export default function AdminJobsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <JobManagerDetailView jobId={id} />
      ))}
    </Suspense>
  );
}
