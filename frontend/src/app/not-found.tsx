import Link from "next/link";

import { BrandLockup } from "@/components/brand";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center gap-5 px-6">
      <BrandLockup />
      <h1 className="font-display text-3xl">This page does not exist</h1>
      <p className="text-md text-muted-foreground">
        The address may be mistyped, or the page may have moved. Your dashboard is one click away.
      </p>
      <Button asChild className="self-start">
        <Link href="/">Go to your dashboard</Link>
      </Button>
    </main>
  );
}
