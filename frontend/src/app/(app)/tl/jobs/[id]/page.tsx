import type { Metadata } from "next";

import { JobManagerDetailView } from "@/components/views/job-manager-detail";

export const metadata: Metadata = { title: "Job" };

export default async function TlJobsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <JobManagerDetailView jobId={id} />;
}
