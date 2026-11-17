/**
 * Runs once when the server starts.
 *
 * The /api rewrite is resolved when the app is built, while route protection
 * (src/proxy.ts) reads API_ORIGIN when the server runs. If the two differ, pages
 * would be protected against one API and send their requests to another, so say
 * so loudly at start-up instead of failing in confusing ways later.
 */
export function register() {
  const built = process.env.BUILT_API_ORIGIN;
  const running = (process.env.API_ORIGIN ?? "http://127.0.0.1:8010").replace(/\/+$/, "");
  if (built && built !== running) {
    console.warn(
      `[config] API_ORIGIN is ${running} at runtime, but this build forwards /api to ${built}. ` +
        "Rebuild with the same API_ORIGIN (docker build --build-arg API_ORIGIN=...), or set the runtime value to match.",
    );
  }
}
