"use client";

import { useLayoutEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { applyTheme } from "@/lib/theme";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        // A 4xx is an answer (no access, not found, invalid): asking again will not change it.
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
          return failureCount < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

function getQueryClient() {
  // A fresh client for every server render; one shared cache in the browser.
  if (typeof window === "undefined") return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}

/** Re-applies the stored theme after React (re)mounts <html> in development Strict Mode. */
function ThemeSync() {
  useLayoutEffect(() => {
    applyTheme();
  }, []);
  return null;
}

export function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={getQueryClient()}>
      <TooltipProvider delayDuration={250}>
        <ThemeSync />
        {children}
        <Toaster position="top-right" offset={{ top: 64, right: 16 }} closeButton />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
