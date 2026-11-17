import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { CandidateDetailView } from "@/components/views/candidate-detail";

export const metadata: Metadata = { title: "Candidate" };

export default function TlCandidateDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <CandidateDetailView candidateId={id} />
      ))}
    </Suspense>
  );
}
