import type { Metadata } from "next";

import { TeamLeadDashboardView } from "@/components/views/team-analytics";

export const metadata: Metadata = { title: "Dashboard" };

export default function TlDashboardPage() {
  return <TeamLeadDashboardView />;
}
