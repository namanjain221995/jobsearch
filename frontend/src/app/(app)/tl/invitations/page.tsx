import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { InvitationsView } from "@/components/views/invitations";

export const metadata: Metadata = { title: "Invitations" };

export default function TlInvitationsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <InvitationsView />
    </Suspense>
  );
}
