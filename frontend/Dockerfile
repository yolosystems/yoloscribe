# ── Build stage ───────────────────────────────────────────────────────────────
FROM node:20-slim AS builder

WORKDIR /app

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./

# VITE_* vars are baked in at build time
ARG VITE_API_BASE=http://localhost:8000
ARG VITE_LOCAL_MODE=true
ARG VITE_SITE=local

RUN VITE_API_BASE=$VITE_API_BASE \
    VITE_LOCAL_MODE=$VITE_LOCAL_MODE \
    VITE_SITE=$VITE_SITE \
    npm run build

# ── Serve stage ───────────────────────────────────────────────────────────────
FROM nginx:alpine

# Serve assets under /local/ so that Vite's base:'./' relative paths resolve
# correctly — index.html at /local/index.html, assets at /local/assets/...
COPY --from=builder /app/dist /usr/share/nginx/html/local
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
