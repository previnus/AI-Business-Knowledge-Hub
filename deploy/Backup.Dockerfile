FROM postgres:16-alpine
RUN apk add --no-cache age \
    && addgroup -S -g 10001 hub && adduser -S -D -H -u 10001 -G hub hub
COPY deploy/backup.sh /usr/local/bin/hub-backup
USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["sh", "/usr/local/bin/hub-backup"]
