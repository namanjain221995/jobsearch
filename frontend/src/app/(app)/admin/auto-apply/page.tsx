import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AutoApplyQueueView } from "@/components/views/auto-apply-queue";

export const metadata: Metadata = { title: "Auto Apply" };

export default function AdminAutoApplyPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AutoApplyQueueView />
    </Suspense>
  );
}
