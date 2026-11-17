import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberDashboardView } from "@/components/views/member-dashboard";

export const metadata: Metadata = { title: "Dashboard" };

export default function MemberDashboardPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberDashboardView />
    </Suspense>
  );
}
