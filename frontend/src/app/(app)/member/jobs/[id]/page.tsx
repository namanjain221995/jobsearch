import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AssignmentDetailPage } from "@/components/views/assignment-detail";

export const metadata: Metadata = { title: "Job" };

export default function MemberJobsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <AssignmentDetailPage assignmentId={id} />
      ))}
    </Suspense>
  );
}
