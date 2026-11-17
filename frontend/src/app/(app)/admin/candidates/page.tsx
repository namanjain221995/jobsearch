import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AdminCandidatesView } from "@/components/views/candidates";

export const metadata: Metadata = { title: "Candidates" };

export default function AdminCandidatesPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AdminCandidatesView />
    </Suspense>
  );
}
