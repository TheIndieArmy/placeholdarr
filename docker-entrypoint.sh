#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /config/logs

chown -R "${PUID}:${PGID}" /config /app

# util-linux setpriv: --clear-groups and --init-groups are mutually exclusive (newer versions exit with an error).
# Use --init-groups so supplementary groups match /etc/group for the dropped UID (typical PUID/PGID behavior).
exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups -- "$@"
