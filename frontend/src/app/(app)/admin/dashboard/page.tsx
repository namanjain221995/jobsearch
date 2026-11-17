import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AdminAnalyticsView } from "@/components/views/admin-analytics";

export const metadata: Metadata = { title: "Dashboard" };

export default function AdminDashboardPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AdminAnalyticsView variant="dashboard" />
    </Suspense>
  );
}
