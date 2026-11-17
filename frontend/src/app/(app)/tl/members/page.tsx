import type { Metadata } from "next";

import { TeamMembersView } from "@/components/views/members";

export const metadata: Metadata = { title: "Team Members" };

export default function TlMembersPage() {
  return <TeamMembersView />;
}
