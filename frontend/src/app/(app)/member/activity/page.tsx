import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberActivityView } from "@/components/views/member-activity";

export const metadata: Metadata = { title: "Activity" };

export default function MemberActivityPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberActivityView />
    </Suspense>
  );
}
