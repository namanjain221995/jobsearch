import type { Metadata } from "next";

import { AuditLogView } from "@/components/views/audit-log";

export const metadata: Metadata = { title: "Activity Log" };

export default function AdminActivityPage() {
  return <AuditLogView />;
}
