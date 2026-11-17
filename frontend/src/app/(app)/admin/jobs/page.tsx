import type { Metadata } from "next";

import { AdminJobsView } from "@/components/views/jobs";

export const metadata: Metadata = { title: "Jobs" };

export default function AdminJobsPage() {
  return <AdminJobsView />;
}
