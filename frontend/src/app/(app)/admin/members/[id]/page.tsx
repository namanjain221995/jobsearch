import type { Metadata } from "next";

import { MemberDetailView } from "@/components/views/member-detail";

export const metadata: Metadata = { title: "Member" };

export default async function AdminMembersDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <MemberDetailView memberId={id} />;
}
