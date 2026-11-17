import type { Metadata } from "next";

import { SearchDetailView } from "@/components/views/search-detail";

export const metadata: Metadata = { title: "Search" };

export default async function TlSearchesDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SearchDetailView searchId={id} />;
}
