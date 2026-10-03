#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /config/logs

# setpriv --init-groups loads supplementary groups from nss; the target UID must exist in /etc/passwd.
# python:3.12-slim has no uid 1000 by default (unlike linuxserver-style images). Create group/user if missing.
if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  if ! getent group "${PGID}" >/dev/null 2>&1; then
    groupadd -g "${PGID}" placeholdarr
  fi
  useradd -u "${PUID}" -g "${PGID}" -M -s /bin/sh placeholdarr
fi

chown -R "${PUID}:${PGID}" /config /app

# util-linux setpriv: --clear-groups and --init-groups are mutually exclusive (newer versions exit with an error).
# Use --init-groups so supplementary groups match /etc/group for the dropped UID (typical PUID/PGID behavior).
#
# Default image CMD is uvicorn with a hardcoded --port 8000. Honor PLACEHOLDARR_PORT /
# PLACEHOLDARR_HOST so compose/env can change the container listen port (#89).
if [ "$1" = "uvicorn" ]; then
  host="${PLACEHOLDARR_HOST:-0.0.0.0}"
  port="${PLACEHOLDARR_PORT:-8000}"
  exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups -- \
    uvicorn main:app --host "$host" --port "$port"
fi

exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups -- "$@"
