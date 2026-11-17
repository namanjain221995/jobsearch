import { Skeleton } from "@/components/ui/skeleton";

/** What a screen shows for the instant before its content is ready: a title bar and a block, no spinner. */
export function PageFallback() {
  return (
    <div role="status" aria-label="Loading" aria-busy="true" className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-4 w-80 max-w-full" />
      </div>
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-72 w-full" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
