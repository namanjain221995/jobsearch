import type { Metadata } from "next";

import { AssignmentsView } from "@/components/views/assignments";

export const metadata: Metadata = { title: "Assignments" };

export default function AdminAssignmentsPage() {
  return <AssignmentsView />;
}
