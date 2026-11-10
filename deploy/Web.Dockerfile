FROM node:24-alpine AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ .
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
RUN addgroup -S -g 10001 hub && adduser -S -D -H -u 10001 -G hub hub \
    && chown -R 10001:10001 /config /data \
    && setcap -r /usr/bin/caddy
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=4 \
    CMD ["wget", "-q", "-T", "3", "-O", "/dev/null", "http://127.0.0.1:9080/healthz"]
