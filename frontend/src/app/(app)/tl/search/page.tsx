import type { Metadata } from "next";

import { SearchForm } from "@/components/search/search-form";

export const metadata: Metadata = { title: "Find Jobs" };

export default function TlSearchPage() {
  return <SearchForm />;
}
