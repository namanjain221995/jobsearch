import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamLeadsView } from "@/components/views/team-leads";

export const metadata: Metadata = { title: "Team Leads" };

export default function AdminTeamLeadsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamLeadsView />
    </Suspense>
  );
}
