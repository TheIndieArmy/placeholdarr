#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /config/logs

chown -R "${PUID}:${PGID}" /config /app

exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups --init-groups -- "$@"
