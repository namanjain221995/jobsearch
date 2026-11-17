import type { Metadata } from "next";

import { MemberJobsView } from "@/components/views/member-jobs";

export const metadata: Metadata = { title: "My Jobs" };

export default function MemberJobsPage() {
  return <MemberJobsView />;
}
