import type { Metadata } from "next";
import { Suspense } from "react";

import { PageFallback } from "@/components/shared/page-fallback";
import { SearchForm } from "@/components/search/search-form";

export const metadata: Metadata = { title: "Find Jobs" };

export default function AdminSearchPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <SearchForm />
    </Suspense>
  );
}
