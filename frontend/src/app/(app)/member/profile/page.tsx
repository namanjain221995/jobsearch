import type { Metadata } from "next";
import { redirect } from "next/navigation";

export const metadata: Metadata = { title: "Settings" };

/**
 * Members used to have a profile and a resume of their own on this page. Those belong to
 * candidates now; what is left of the page (name, password, theme) is /member/settings.
 * Old bookmarks and links still arrive here and are sent on.
 */
export default function MemberProfilePage() {
  redirect("/member/settings");
}
