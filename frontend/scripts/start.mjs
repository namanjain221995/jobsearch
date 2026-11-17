// Runs the production build made by `npm run build`.
//
// The build uses Next.js "standalone" output: a self-contained server in
// .next/standalone. That server serves the static assets and the public folder
// only when they sit beside it, so they are copied there first (the Dockerfile
// does the same with COPY).
//
//   PORT   port to listen on        (default 3010; never 3000 on this machine)
//   HOST   address to listen on     (default 127.0.0.1; use 0.0.0.0 to accept other machines)
//   NEXT_DIST_DIR  build directory (default .next); must be the value used for `npm run build`
//   API_ORIGIN is fixed at build time for the /api rewrite; see .env.example.
import { cpSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = process.env.NEXT_DIST_DIR || ".next";      // same variable as next.config.ts
const standalone = path.join(root, dist, "standalone");
const server = path.join(standalone, "server.js");

if (!existsSync(server)) {
  console.error("No production build found. Run `npm run build` first.");
  process.exit(1);
}

cpSync(path.join(root, dist, "static"), path.join(standalone, dist, "static"), { recursive: true });
if (existsSync(path.join(root, "public"))) {
  cpSync(path.join(root, "public"), path.join(standalone, "public"), { recursive: true });
}

process.env.NODE_ENV = "production";
process.env.PORT ??= "3010";
// The standalone server reads HOSTNAME, which many shells set to the machine's name.
process.env.HOSTNAME = process.env.HOST ?? "127.0.0.1";
process.chdir(standalone);
await import(pathToFileURL(server).href);
