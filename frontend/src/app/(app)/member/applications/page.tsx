import type { Metadata } from "next";

import { MemberApplicationsView } from "@/components/views/member-jobs";

export const metadata: Metadata = { title: "Applications" };

export default function MemberApplicationsPage() {
  return <MemberApplicationsView />;
}
