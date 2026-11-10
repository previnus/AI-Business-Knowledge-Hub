#!/bin/sh
set -eu
set -o pipefail
umask 077
: "${AGE_RECIPIENT:?Set the age public recipient}"
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
encrypted="$(mktemp /backups/.hub-XXXXXX)"
trap 'rm -f -- "$encrypted"' EXIT HUP INT TERM
pg_dump --host db --username hub_owner --dbname knowledgehub --format custom --no-password \
    | age -r "$AGE_RECIPIENT" > "$encrypted"
mv -- "$encrypted" "/backups/knowledgehub-$stamp.dump.age"
