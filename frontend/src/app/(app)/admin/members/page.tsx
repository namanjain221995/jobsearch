import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AdminMembersView } from "@/components/views/members";

export const metadata: Metadata = { title: "Members" };

export default function AdminMembersPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AdminMembersView />
    </Suspense>
  );
}
