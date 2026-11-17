import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberDetailView } from "@/components/views/member-detail";

export const metadata: Metadata = { title: "Member" };

export default function AdminMembersDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <MemberDetailView memberId={id} />
      ))}
    </Suspense>
  );
}
