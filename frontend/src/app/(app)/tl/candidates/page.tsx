import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamCandidatesView } from "@/components/views/candidates";

export const metadata: Metadata = { title: "Candidates" };

export default function TlCandidatesPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamCandidatesView />
    </Suspense>
  );
}
