#!/bin/sh
# Code by AkinoAlice@TyrantRey
#
# The container's entry point (DESIGN/v0-5-0.md §10.3): make /data a root on
# the first run, drop the lock an earlier run of *this* container left
# behind, then exec the command (`tfs start --log-console` by default).
set -eu

root="${TFS_ROOT:-/data}"
bind="${TFS_BIND:-0.0.0.0}"

if [ ! -d "$root" ] || [ ! -w "$root" ]; then
    echo "error: $root is not a writable directory for uid $(id -u)." >&2
    echo "  Create the folder on the host before the first start and make it" >&2
    echo "  writable by that uid (PUID/PGID in docker-compose.yml)." >&2
    exit 1
fi

if [ ! -f "$root/.tfs/config.toml" ]; then
    echo "initializing a root at $root"
    tfs init "$root"
    # The daemon's default bind is the loopback, which a published port never
    # reaches: inside a container the address that works is 0.0.0.0.
    sed -i "s/^bind = \"127.0.0.1\"/bind = \"$bind\"/" "$root/.tfs/config.toml"
elif grep -q '^bind = "127.0.0.1"' "$root/.tfs/config.toml"; then
    echo "warning: [daemon] bind is 127.0.0.1 in $root/.tfs/config.toml: the published port will not reach the daemon (set it to 0.0.0.0)" >&2
fi

# A lock stamped with this container's hostname was written by an earlier run
# of this same container (a crash, a SIGKILL, a host reboot). The pid
# namespace started fresh with this process, so nothing holds it now; a lock
# from any other host is left for the daemon to judge.
lock="$root/.tfs/lock"
if [ -f "$lock" ] && grep -q "\"hostname\": \"$(hostname)\"" "$lock"; then
    echo "removing the lock left by an earlier run of this container ($(hostname))"
    rm -f "$lock"
fi

exec "$@"
