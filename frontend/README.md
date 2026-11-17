# AI Job Finder frontend

The web interface of the AI Job Finder portal: a Next.js (App Router) application for three roles
(Admin, Team Lead, Member) on top of the FastAPI backend in `../app/portal`.

## Run it

```bash
npm install
cp .env.example .env.local        # set API_ORIGIN if the API is not on http://127.0.0.1:8010
npm run dev                       # http://127.0.0.1:3010
```

Port 3000 belongs to another application on the development machine. The dev server listens on 3010,
and on another port with `npm run dev -- --port 3011`.

Against the deterministic demo API (synthetic data, see `../tests/e2e/server.py`):

```bash
API_ORIGIN=http://127.0.0.1:8011 npm run dev -- --port 3011
```

| Script | What it does |
| --- | --- |
| `npm run dev` | Development server on `127.0.0.1:3010` |
| `npm run build` | Production build (standalone output in `.next/standalone`) |
| `npm run start` | Serves the production build on `127.0.0.1:3010` (`PORT` and `HOST` change that) |
| `npm run lint` | ESLint |
| `npm run typecheck` | TypeScript, no emit |
| `npm run gen:api` | Regenerates `src/lib/api-types.ts` from `../openapi.json` |

## How it talks to the API

- The browser only calls relative `/api/...` URLs. `next.config.ts` rewrites them to
  `${API_ORIGIN}/api/...`, so the session cookie is same-origin and the API address never reaches
  client code.
- `API_ORIGIN` is a server-side variable. The rewrite is resolved **when the app is built**, so build
  with the value the deployment uses. The server prints a warning at start-up if the runtime value
  differs from the built one.
- Sign-in sets an HttpOnly session cookie (`jf_session`). Nothing is kept in `localStorage` except the
  theme choice.
- Every POST, PATCH, PUT and DELETE sends `X-CSRF-Token` with the value of the `jf_csrf` cookie. All of
  this lives in one helper, `apiFetch` in `src/lib/api.ts`.
- `src/proxy.ts` (the Next.js 16 name for middleware) asks `GET /api/auth/me` who the visitor is and
  redirects to the sign-in page or to the visitor's own area. It is a convenience: the API checks
  every permission itself, and a 403 is shown as an "Access denied" state.

Response and request types are generated from the OpenAPI contract (`src/lib/api-types.ts`) and named
in `src/lib/types.ts`. Nothing is typed by hand. After the contract changes, run `npm run gen:api` and
`npm run typecheck`.

### Three settings that are easy to break

- **Upload size.** Because `src/proxy.ts` exists, Next.js keeps a copy of every request body and cuts
  it off at 10 MB unless told otherwise; a cut-off upload hangs in the `/api` rewrite for 30 seconds and
  ends as a 500. `experimental.proxyClientMaxBodySize` in `next.config.ts` is set to `12mb` so that
  everything the API accepts (resumes up to 10 MB plus the multipart framing) passes whole. Keep it
  above the API's `max_upload_bytes`.
- **Content Security Policy.** Production builds send a policy that allows only this origin
  (`next.config.ts`). Anything loaded from another host (a font, an analytics script, an image) will be
  blocked until the policy names it.
- **Client addresses.** The `/api` rewrite does not add `X-Forwarded-For`, so the API sees every
  request as coming from this server. If the API should limit sign-in attempts per address or log
  real addresses, put a reverse proxy in front of this app that sets the header, and turn on the API's
  `trust_forwarded_for`.

## Docker

```bash
docker build --build-arg API_ORIGIN=http://backend:8010 -t jobfinder-frontend .
docker run -p 3010:3000 -e API_ORIGIN=http://backend:8010 jobfinder-frontend
```

Multi-stage build, standalone output, runs as a non-root user and listens on port 3000 inside the
container. Pass `API_ORIGIN` both as a build argument and as an environment variable, with the same
value.

## Where things are

| Path | Contents |
| --- | --- |
| `src/app/(public)` | Sign in, forgot and reset password, accept invitation |
| `src/app/(app)` | Signed-in areas: `/admin`, `/tl`, `/member` |
| `src/components/shell` | `DashboardShell`, `RoleSidebar`, `Header`, `NotificationCenter` |
| `src/components/shared` | `DataTable`, `KPIStatCard`, status badge and stage rail, empty / loading / error states, form fields, date and search inputs |
| `src/components/charts` | Charts (Recharts) and the pipeline funnel |
| `src/components/jobs` | `JobCard`, `JobDetails`, description rendering, match, tracking, status dialog, watermark, job forms |
| `src/components/people` | Accounts (Team Leads and members, the employees): `MemberTable`, `TeamPerformanceTable`, `InvitationDialog`, account actions |
| `src/components/candidates` | Candidates (the people who need a job; records, not accounts): table, picker, add/edit/assign dialogs, profile form, resume upload and download |
| `src/components/member` | The member area: candidate cards and a candidate's job list |
| `src/components/search` | `SearchForm`, the progress window and its indicator |
| `src/components/views` | One component per screen, shared between roles |
| `src/lib` | `apiFetch`, generated API types, TanStack Query hooks, status and metric definitions, the API's input rules mirrored for forms (`forms.ts`, `date-range.ts`) |

Design tokens (colours, type, radii) are in `src/app/globals.css`. Application statuses use one set
of colour tokens for badges, stage rails and chart series.
