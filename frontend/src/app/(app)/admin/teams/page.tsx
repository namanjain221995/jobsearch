import type { Metadata } from "next";

import { TeamsView } from "@/components/views/teams";

export const metadata: Metadata = { title: "Teams" };

export default function AdminTeamsPage() {
  return <TeamsView />;
}
