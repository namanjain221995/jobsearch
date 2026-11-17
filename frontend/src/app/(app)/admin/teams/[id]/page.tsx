import type { Metadata } from "next";

import { TeamDetailView } from "@/components/views/teams";

export const metadata: Metadata = { title: "Team" };

export default async function AdminTeamsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <TeamDetailView teamId={id} />;
}
