import type { Metadata } from "next";

import { TeamLeadSettingsView } from "@/components/views/account-settings";

export const metadata: Metadata = { title: "Settings" };

export default function TlSettingsPage() {
  return <TeamLeadSettingsView />;
}
