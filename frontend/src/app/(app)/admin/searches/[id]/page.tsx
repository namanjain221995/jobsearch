import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { SearchDetailView } from "@/components/views/search-detail";

export const metadata: Metadata = { title: "Search" };

export default function AdminSearchesDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<PageFallback />}>
      {params.then(({ id }) => (
        <SearchDetailView searchId={id} />
      ))}
    </Suspense>
  );
}
