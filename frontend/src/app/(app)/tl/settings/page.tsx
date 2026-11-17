import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { TeamLeadSettingsView } from "@/components/views/account-settings";

export const metadata: Metadata = { title: "Settings" };

export default function TlSettingsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <TeamLeadSettingsView />
    </Suspense>
  );
}
