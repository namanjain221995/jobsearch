import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberProfileView } from "@/components/views/member-profile";

export const metadata: Metadata = { title: "Profile" };

export default function MemberProfilePage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberProfileView />
    </Suspense>
  );
}
