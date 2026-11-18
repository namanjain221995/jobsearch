"use client";

import { RunsList } from "@/components/auto-apply/runs-list";
import { PageHeader } from "@/components/shared/page-header";
import { Spinner } from "@/components/ui/spinner";
import { ACTIVE_RUN_STATUSES, useAutoApplyRuns } from "@/lib/auto-apply";

/** Admin "Auto Apply": every run across candidates that needs a person, with batch approval. */
export function AutoApplyQueueView() {
  const active = useAutoApplyRuns({ status: ACTIVE_RUN_STATUSES, page: 1, page_size: 1 });
  const working = active.data?.total ?? 0;

  return (
    <>
      <PageHeader
        title="Auto Apply"
        description="Applications the portal prepared that need input, review or attention. Open a run to preview it; only previewed runs can be approved together."
        meta={
          working > 0 ? (
            <span className="inline-flex items-center gap-1.5 text-2sm text-muted-foreground" role="status">
              <Spinner className="size-3.5" />
              {working} being prepared or submitted
            </span>
          ) : undefined
        }
      />
      <RunsList queue fast={working > 0} />
    </>
  );
}
