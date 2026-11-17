import type { Metadata } from "next";

import { MemberActivityView } from "@/components/views/member-activity";

export const metadata: Metadata = { title: "Activity" };

export default function MemberActivityPage() {
  return <MemberActivityView />;
}
