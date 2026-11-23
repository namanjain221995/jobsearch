import type { Metadata } from "next";

import { LandingPage } from "@/components/landing/landing-page";

/**
 * "/" is the public front page for visitors who are not signed in. Signed-in people
 * never see it: the proxy (src/proxy.ts) sends them to their own dashboard first.
 */

const TITLE = "AI Job Finder: verified US jobs for every candidate";
const DESCRIPTION =
  "AI agents search the open web and employers' career sites, check every opening on the employer's live page and match US jobs to each candidate's resume, with a written reason, for staffing teams.";

export const metadata: Metadata = {
  metadataBase: new URL("https://jobs.techsarasolutions.com"),
  title: { absolute: TITLE },
  description: DESCRIPTION,
  alternates: { canonical: "/" },
  // The signed-in app stays out of search engines (root layout); this one public page may be listed.
  robots: { index: true, follow: true },
  openGraph: {
    type: "website",
    url: "/",
    siteName: "AI Job Finder",
    title: TITLE,
    description: DESCRIPTION,
    locale: "en_US",
  },
  twitter: { card: "summary", title: TITLE, description: DESCRIPTION },
};

export default function RootPage() {
  return <LandingPage />;
}
