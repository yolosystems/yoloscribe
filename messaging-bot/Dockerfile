FROM node:22-alpine AS builder
WORKDIR /app
COPY messaging-bot/package.json messaging-bot/package-lock.json ./
RUN npm ci
COPY messaging-bot/tsconfig.json ./
COPY messaging-bot/src ./src
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY messaging-bot/package.json messaging-bot/package-lock.json ./
RUN npm ci --omit=dev
COPY --from=builder /app/dist ./dist
RUN addgroup -S bot && adduser -S bot -G bot
USER bot
CMD ["node", "dist/index.js"]
