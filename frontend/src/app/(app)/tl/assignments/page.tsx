import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AssignmentsView } from "@/components/views/assignments";

export const metadata: Metadata = { title: "Assigned Jobs" };

export default function TlAssignmentsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AssignmentsView />
    </Suspense>
  );
}
