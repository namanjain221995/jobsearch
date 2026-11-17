"use client";

import Link from "next/link";
import { LogOut, Settings } from "lucide-react";
import { toast } from "sonner";

import { useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { errorMessage, stopApi } from "@/lib/api";
import { initials, roleLabel } from "@/lib/format";
import { ACCOUNT_PATH } from "@/lib/nav";
import { useLogout } from "@/lib/queries";

export function ProfileMenu() {
  const me = useMe();
  const logout = useLogout();

  const signOut = () => {
    logout.mutate(undefined, {
      onSuccess: () => {
        stopApi();
        // A full page load on purpose: signing out must drop every cached screen and query of this
        // session, which a client-side navigation would keep (see the Next.js guide on preserving UI state).
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination
        window.location.href = "/login";
      },
      onError: (error) => toast.error(errorMessage(error, "Could not sign out. Try again.")),
    });
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="h-8 gap-2 px-1.5" aria-label={`Account menu for ${me.name}`}>
          <span
            aria-hidden="true"
            className="grid size-6 place-items-center rounded-[5px] bg-primary font-label text-[0.6875rem] text-primary-foreground"
          >
            {initials(me.name)}
          </span>
          <span className="hidden max-w-36 truncate text-sm sm:inline">{me.name}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-60">
        <DropdownMenuLabel className="flex flex-col gap-0.5 font-normal">
          <span className="font-label text-sm text-foreground">{me.name}</span>
          <span className="truncate text-xs text-muted-foreground">{me.email}</span>
          <span className="text-xs text-muted-foreground">
            {roleLabel(me.role)}
            {me.team ? `, ${me.team.name}` : ""}
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href={ACCOUNT_PATH[me.role]}>
            <Settings />
            Settings
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={logout.isPending || logout.isSuccess}
          onSelect={(event) => {
            event.preventDefault();
            signOut();
          }}
        >
          <LogOut />
          {logout.isPending || logout.isSuccess ? "Signing out" : "Sign out"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
