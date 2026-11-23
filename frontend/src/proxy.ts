import { NextResponse, type NextRequest } from "next/server";

/**
 * Route protection (Next.js 16 "proxy", formerly middleware).
 *
 * For every page that is not public, the API is asked who the visitor is
 * (GET /api/auth/me with the visitor's own cookies):
 *   - not signed in            -> /login?next=<where they were going>
 *   - signed in, another role's area (/admin, /tl, /member) -> their own home
 *   - "/"                      -> signed in: their home; otherwise the public landing page
 *   - signed in and on /login  -> their home
 *
 * This only decides which screen to show. The API enforces every permission
 * itself on each request, so nothing here is a security boundary.
 */

const API_ORIGIN = (process.env.API_ORIGIN ?? "http://127.0.0.1:8010").replace(/\/+$/, "");
const PUBLIC_PATHS = ["/login", "/forgot-password", "/reset-password", "/invite/accept"];
const ROLE_AREAS: Record<string, string> = { ADMIN: "/admin", TEAM_LEAD: "/tl", MEMBER: "/member" };
const AREA_PREFIXES = Object.values(ROLE_AREAS);
const ME_TIMEOUT_MS = 5000;

type Session =
  | { state: "signed-in"; role: string; home: string }
  | { state: "signed-out" }
  /** The API could not be reached: do not pretend the visitor is signed out. */
  | { state: "unknown" };

const within = (pathname: string, prefix: string) => pathname === prefix || pathname.startsWith(`${prefix}/`);

async function loadSession(request: NextRequest): Promise<Session> {
  try {
    const response = await fetch(`${API_ORIGIN}/api/auth/me`, {
      headers: { cookie: request.headers.get("cookie") ?? "", accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(ME_TIMEOUT_MS),
    });
    if (response.status === 401 || response.status === 403) return { state: "signed-out" };
    if (!response.ok) return { state: "unknown" };
    const me: unknown = await response.json();
    if (me && typeof me === "object") {
      const { role, home } = me as { role?: unknown; home?: unknown };
      // `home` is used as a redirect target, so it must be one of this app's own role areas.
      if (typeof role === "string" && typeof home === "string" && AREA_PREFIXES.some((p) => within(home, p))) {
        return { state: "signed-in", role, home };
      }
    }
    return { state: "unknown" };
  } catch {
    return { state: "unknown" };
  }
}

function redirect(request: NextRequest, pathname: string, search = ""): NextResponse {
  const url = request.nextUrl.clone();
  url.pathname = pathname;
  url.search = search;
  const response = NextResponse.redirect(url);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const isLogin = within(pathname, "/login");
  const isPublic = PUBLIC_PATHS.some((p) => within(pathname, p));

  // Password reset and invitation links work whether or not someone is signed in.
  if (isPublic && !isLogin) return NextResponse.next();

  const session = await loadSession(request);

  if (isLogin) {
    return session.state === "signed-in" ? redirect(request, session.home) : NextResponse.next();
  }

  if (session.state === "unknown") {
    // The page shell explains that the service is unreachable and offers a retry
    // ("/" shows the landing page, whose links lead to the sign-in page).
    return NextResponse.next();
  }

  if (session.state === "signed-out") {
    // The landing page is the front door for visitors without a session.
    if (pathname === "/") return NextResponse.next();
    return redirect(request, "/login", `?next=${encodeURIComponent(pathname + search)}`);
  }

  if (pathname === "/") return redirect(request, session.home);

  const area = AREA_PREFIXES.find((prefix) => within(pathname, prefix));
  if (area && area !== ROLE_AREAS[session.role]) return redirect(request, session.home);

  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Every path except the API (forwarded to the backend by next.config rewrites),
     * Next.js internals and files with an extension (icons, fonts, images).
     */
    "/((?!api/|_next/|.*\\.[\\w]+$).*)",
  ],
};
