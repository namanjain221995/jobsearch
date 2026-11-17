import type { Metadata } from "next";

import { SearchHistoryView } from "@/components/views/search-history";

export const metadata: Metadata = { title: "Search History" };

export default function AdminSearchesPage() {
  return <SearchHistoryView />;
}
