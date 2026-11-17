import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberApplicationsView } from "@/components/views/member-jobs";

export const metadata: Metadata = { title: "Applications" };

export default function MemberApplicationsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberApplicationsView />
    </Suspense>
  );
}
