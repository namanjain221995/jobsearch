import { redirect } from "next/navigation";

/**
 * "/" has no screen of its own. The proxy sends visitors to their dashboard or to
 * the sign-in page before this renders; this is the fallback if it ever does.
 */
export default function RootPage() {
  redirect("/login");
}
