import type { Metadata } from "next";

import { TeamLeadsView } from "@/components/views/team-leads";

export const metadata: Metadata = { title: "Team Leads" };

export default function AdminTeamLeadsPage() {
  return <TeamLeadsView />;
}
