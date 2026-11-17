import type { ReactNode } from "react";
import Link from "next/link";

import { BrandLockup } from "@/components/brand";

/**
 * Signed-out screens. The form sits on paper; beside it, on wide screens, an ink
 * panel shows the one idea the product is built around: a job moving through
 * five stages, the pipeline narrowing as it goes.
 */
const STAGES = [
  { label: "Assigned", width: "100%", tone: "oklch(0.86 0.06 268)" },
  { label: "Viewed", width: "78%", tone: "oklch(0.78 0.1 268)" },
  { label: "Applied", width: "54%", tone: "oklch(0.7 0.14 268)" },
  { label: "Interview", width: "30%", tone: "oklch(0.92 0.03 268)" },
  { label: "Offer", width: "14%", tone: "oklch(0.78 0.17 150)" },
];

export default function PublicLayout({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-svh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
      <div className="flex min-h-svh flex-col px-5 py-6 sm:px-10 lg:px-14">
        <header>
          <Link href="/login" className="inline-flex rounded-sm" aria-label="AI Job Finder, sign in">
            <BrandLockup />
          </Link>
        </header>
        <main id="main" className="flex flex-1 items-center py-10">
          <div className="w-full max-w-[25rem]">{children}</div>
        </main>
        <footer className="text-2sm text-muted-foreground">
          Accounts are created by invitation. Ask your Team Lead or Admin for access.
        </footer>
      </div>

      <aside
        aria-hidden="true"
        className="relative hidden overflow-hidden bg-[oklch(0.26_0.12_268)] text-[oklch(0.97_0.01_268)] lg:flex lg:flex-col lg:justify-center lg:px-16 xl:px-24"
      >
        <ol className="flex flex-col gap-7">
          {STAGES.map((stage, index) => (
            <li key={stage.label} className="flex flex-col gap-2.5">
              <span className="font-display text-4xl leading-none xl:text-5xl">{stage.label}</span>
              <span className="block h-2.5 rounded-[3px] bg-white/8">
                <span
                  className="block h-full origin-left animate-rail-fill rounded-[3px]"
                  style={{ width: stage.width, backgroundColor: stage.tone, animationDelay: `${150 + index * 110}ms` }}
                />
              </span>
            </li>
          ))}
        </ol>
        <p className="mt-14 max-w-sm text-md text-[oklch(0.85_0.04_268)]">
          Every assigned job, followed from the first look to the offer.
        </p>
      </aside>
    </div>
  );
}
