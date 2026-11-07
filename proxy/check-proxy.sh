#!/bin/sh
set -eu
# Liveness plus a local listener check; no Internet request or extra package.
squid -f /tmp/squid.conf -k check >/dev/null 2>&1
exec bash -c 'exec 3<>/dev/tcp/127.0.0.1/3128'
