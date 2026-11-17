# syntax=docker/dockerfile:1
#
# AI Job Finder frontend (Next.js, standalone output).
#
#   docker build --build-arg API_ORIGIN=http://backend:8010 -t jobfinder-frontend .
#   docker run -p 3010:3000 -e API_ORIGIN=http://backend:8010 jobfinder-frontend
#
# API_ORIGIN is needed twice. The /api rewrite in next.config.ts is resolved when
# the app is built, so the address is a build argument. The route protection in
# src/proxy.ts reads it again when the server runs, so it is also an environment
# variable. Give both the same value.

FROM node:20-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1

# ---- dependencies: only re-run when the lockfile changes
FROM base AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- build
FROM base AS builder
WORKDIR /app
ARG API_ORIGIN=http://127.0.0.1:8010
ENV API_ORIGIN=${API_ORIGIN}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runtime: the standalone server and the static assets, nothing else
FROM base AS runner
WORKDIR /app
ARG API_ORIGIN=http://127.0.0.1:8010
ENV NODE_ENV=production \
    API_ORIGIN=${API_ORIGIN} \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs

COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

# The sign-in page is public and does not need the API to answer.
HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/login || exit 1

CMD ["node", "server.js"]
