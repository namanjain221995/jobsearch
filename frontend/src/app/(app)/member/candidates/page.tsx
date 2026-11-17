import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberCandidatesView } from "@/components/views/member-candidates";

export const metadata: Metadata = { title: "My Candidates" };

export default function MemberCandidatesPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberCandidatesView />
    </Suspense>
  );
}
