#!/bin/sh
# SolarWatch container start-up.
#  1. On first start, put a config.yaml in the data folder for you to edit.
#  2. Make the data folder writable by the app user (PUID/PGID, default 1000).
#  3. Drop root and run the app as that user.
set -e

CONFIG="${SOLARWATCH_CONFIG:-/app/data/config.yaml}"
DATA_DIR="${SOLARWATCH_DATA:-/app/data}"   # only this folder is ever chown'ed
EXAMPLE="${SOLARWATCH_EXAMPLE:-/app/config.example.yaml}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p "$DATA_DIR" "$(dirname "$CONFIG")"

if [ ! -e "$CONFIG" ]; then
    cp "$EXAMPLE" "$CONFIG"
    echo "SolarWatch: created $CONFIG from the example."
    echo "SolarWatch: edit it (in the data folder on your host) to keep only the platforms you use,"
    echo "SolarWatch: then restart the container. Passwords and tokens go in the .env file."
elif [ -d "$CONFIG" ]; then
    echo "SolarWatch: $CONFIG is a folder, not a file." >&2
    echo "SolarWatch: this happens when a config file is mounted that doesn't exist on the host." >&2
    echo "SolarWatch: remove that folder and the config.yaml volume line, then start again." >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    chown -R "$PUID:$PGID" "$DATA_DIR"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

exec "$@"
