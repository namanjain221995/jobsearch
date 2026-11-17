import type { Metadata } from "next";

import { AssignmentDetailPage } from "@/components/views/assignment-detail";

export const metadata: Metadata = { title: "Job" };

export default async function MemberJobsDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AssignmentDetailPage assignmentId={id} back={{ href: "/member/jobs", label: "My Jobs" }} />;
}
