import type { Metadata } from "next";
import { Suspense } from "react";

import { AcceptInvitation } from "@/components/auth/accept-invitation";
import { LoadingSkeleton } from "@/components/shared/states";

export const metadata: Metadata = { title: "Accept your invitation" };

export default function AcceptInvitationPage() {
  return (
    <Suspense fallback={<LoadingSkeleton variant="form" count={4} />}>
      <AcceptInvitation />
    </Suspense>
  );
}
