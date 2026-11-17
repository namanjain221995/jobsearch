import type { Metadata } from "next";

import { AdminAnalyticsView } from "@/components/views/admin-analytics";

export const metadata: Metadata = { title: "Dashboard" };

export default function AdminDashboardPage() {
  return <AdminAnalyticsView variant="dashboard" />;
}
