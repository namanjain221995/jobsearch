import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { AdminSettingsView } from "@/components/views/admin-settings";

export const metadata: Metadata = { title: "Settings" };

export default function AdminSettingsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <AdminSettingsView />
    </Suspense>
  );
}
