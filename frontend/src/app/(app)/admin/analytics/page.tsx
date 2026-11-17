import type { Metadata } from "next";

import { AdminAnalyticsView } from "@/components/views/admin-analytics";

export const metadata: Metadata = { title: "Analytics" };

export default function AdminAnalyticsPage() {
  return <AdminAnalyticsView variant="analytics" />;
}
