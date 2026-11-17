import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { SearchHistoryView } from "@/components/views/search-history";

export const metadata: Metadata = { title: "Search History" };

export default function TlSearchesPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <SearchHistoryView />
    </Suspense>
  );
}
