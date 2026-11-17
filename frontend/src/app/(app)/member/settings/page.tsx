import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { MemberSettingsView } from "@/components/views/account-settings";

export const metadata: Metadata = { title: "Settings" };

export default function MemberSettingsPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <MemberSettingsView />
    </Suspense>
  );
}
