import type { Metadata } from "next";

import { AdminMembersView } from "@/components/views/members";

export const metadata: Metadata = { title: "Members" };

export default function AdminMembersPage() {
  return <AdminMembersView />;
}
