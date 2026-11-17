/**
 * The one place the app gets `z` from.
 *
 * Zod speeds up object schemas by compiling them with `new Function`, and it
 * finds out whether that is allowed by trying it when a schema is defined. The
 * production Content Security Policy does not allow eval, and the browser
 * reports every such attempt as a violation even though Zod catches the error.
 * Turning the compiler off here, before any schema exists, means nothing is
 * tried. Forms validate a handful of fields; the compiler is not missed.
 *
 * Import `z` from "@/lib/zod", never from "zod" (the lint rule says so): a
 * module that defines a schema must have run this file first.
 */
import { z } from "zod";

z.config({ jitless: true });

export { z };
