import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberJobsView } from "@/components/views/member-jobs";

export const metadata: Metadata = { title: "Jobs" };

export default function MemberJobsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberJobsView />
    </Suspense>
  );
}
