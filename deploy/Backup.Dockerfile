FROM postgres:16-alpine
RUN apk add --no-cache age
COPY deploy/backup.sh /usr/local/bin/hub-backup
ENTRYPOINT ["sh", "/usr/local/bin/hub-backup"]
