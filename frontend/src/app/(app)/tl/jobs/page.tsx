import type { Metadata } from "next";

import { TeamJobsView } from "@/components/views/jobs";

export const metadata: Metadata = { title: "Pending Jobs" };

export default function TlJobsPage() {
  return <TeamJobsView />;
}
