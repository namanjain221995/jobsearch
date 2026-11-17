import type { MouseEvent } from "react";

/**
 * For drawers and popovers: following a link to another screen leaves the page,
 * so whatever was open closes with it instead of sitting on top of the destination.
 */
export function closeOnInternalLink(onOpenChange: (open: boolean) => void) {
  return (event: MouseEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest('a[href^="/"]:not([target="_blank"])')) onOpenChange(false);
  };
}
