"use client";

import { useEffect } from "react";

import { BrandLockup } from "@/components/brand";
import { Button } from "@/components/ui/button";

/** Last line of defence: something broke while rendering a screen. Say so and offer a way forward. */
export default function RouteError({ error, retry }: { error: unknown; retry: () => void }) {
  useEffect(() => {
    console.error("A screen could not be rendered", error);
  }, [error]);

  return (
    <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center gap-5 px-6">
      <BrandLockup />
      <h1 className="font-display text-3xl">This screen could not be shown</h1>
      <p className="text-md text-muted-foreground">
        Something went wrong while drawing it. Your data is not affected. Try again, and if it keeps happening, reload the page.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => retry()}>Try again</Button>
        <Button variant="outline" onClick={() => window.location.reload()}>
          Reload the page
        </Button>
      </div>
    </main>
  );
}
