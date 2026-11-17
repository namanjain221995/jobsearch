import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberCandidateView } from "@/components/views/member-candidates";

export const metadata: Metadata = { title: "Candidate" };

export default function MemberCandidateDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <MemberCandidateView candidateId={id} />
      ))}
    </Suspense>
  );
}
