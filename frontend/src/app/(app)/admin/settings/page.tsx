import type { Metadata } from "next";

import { AdminSettingsView } from "@/components/views/admin-settings";

export const metadata: Metadata = { title: "Settings" };

export default function AdminSettingsPage() {
  return <AdminSettingsView />;
}
