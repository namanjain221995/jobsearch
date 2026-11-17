import type { Metadata } from "next";

import { InvitationsView } from "@/components/views/invitations";

export const metadata: Metadata = { title: "Invitations" };

export default function TlInvitationsPage() {
  return <InvitationsView />;
}
