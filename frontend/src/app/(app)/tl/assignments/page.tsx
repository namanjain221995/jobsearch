import type { Metadata } from "next";

import { AssignmentsView } from "@/components/views/assignments";

export const metadata: Metadata = { title: "Assigned Jobs" };

export default function TlAssignmentsPage() {
  return <AssignmentsView />;
}
