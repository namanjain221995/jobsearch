import { PageFallback } from "@/components/shared/page-fallback";

/**
 * The Suspense fallback for every signed-in page. Pages read the signed-in user
 * and the URL in the browser; until those are ready this placeholder holds the
 * page's place, so navigating between screens is instant.
 */
export default function Loading() {
  return <PageFallback />;
}
