#!/bin/sh
# Run as root on the LXC. No Proxmox or other-stack changes.
set -eu
umask 077
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$script_dir/backup.py" "$@"
