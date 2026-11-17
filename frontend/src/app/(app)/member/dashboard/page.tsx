import type { Metadata } from "next";

import { MemberDashboardView } from "@/components/views/member-dashboard";

export const metadata: Metadata = { title: "Dashboard" };

export default function MemberDashboardPage() {
  return <MemberDashboardView />;
}
