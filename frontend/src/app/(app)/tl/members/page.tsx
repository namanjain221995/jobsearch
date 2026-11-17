import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamMembersView } from "@/components/views/members";

export const metadata: Metadata = { title: "Team Members" };

export default function TlMembersPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamMembersView />
    </Suspense>
  );
}
