"use client";

import { use } from "react";
import { browser } from "react-dom";
import { useSuspenseQuery } from "@tanstack/react-query";

import { ROLE_AREA } from "@/lib/nav";
import { meQueryOptions } from "@/lib/queries";
import type { Me } from "@/lib/types";

/**
 * The signed-in user (GET /api/auth/me).
 *
 * Who is signed in is only known in the browser: the server renders a static
 * shell and never sees the session. So this hook suspends, first until the code
 * runs in the browser and then until the user has been fetched. Every caller
 * therefore sits under a <Suspense> boundary: `loading.tsx` provides one for
 * each page, the shell provides them for the sidebar and the header.
 */
export function useMe(): Me {
  use(browser("The signed-in user is only known in the browser."));
  return useSuspenseQuery(meQueryOptions).data;
}

/** "/admin", "/tl" or "/member" for the signed-in user. */
export function useArea(): string {
  return ROLE_AREA[useMe().role];
}
