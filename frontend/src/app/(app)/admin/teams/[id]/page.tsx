import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamDetailView } from "@/components/views/teams";

export const metadata: Metadata = { title: "Team" };

export default function AdminTeamsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <TeamDetailView teamId={id} />
      ))}
    </Suspense>
  );
}
