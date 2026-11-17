import { PageFallback } from "@/components/shared/page-fallback";

/**
 * Fallback for a signed-in page while the route itself is loading. Each page
 * also wraps its view in its own <Suspense> with this same placeholder: views
 * read the signed-in user and the URL in the browser, and that boundary has to
 * sit inside the page for navigation between screens to be instant.
 */
export default function Loading() {
  return <PageFallback />;
}
