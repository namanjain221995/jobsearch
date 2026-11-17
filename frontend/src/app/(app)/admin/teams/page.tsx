import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamsView } from "@/components/views/teams";

export const metadata: Metadata = { title: "Teams" };

export default function AdminTeamsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamsView />
    </Suspense>
  );
}
