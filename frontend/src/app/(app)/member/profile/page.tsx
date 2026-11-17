import type { Metadata } from "next";

import { MemberProfileView } from "@/components/views/member-profile";

export const metadata: Metadata = { title: "Profile" };

export default function MemberProfilePage() {
  return <MemberProfileView />;
}
