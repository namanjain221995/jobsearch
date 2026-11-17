import type { Metadata } from "next";

import { TeamLeadAnalyticsView } from "@/components/views/team-analytics";

export const metadata: Metadata = { title: "Analytics" };

export default function TlAnalyticsPage() {
  return <TeamLeadAnalyticsView />;
}
