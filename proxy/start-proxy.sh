#!/bin/sh
set -eu
umask 077

# The test portal is the sole permitted private destination and only on 8000.
# Validate before substitution so an environment value cannot inject Squid ACLs.
fixture_ip=${FIXTURE_IP:-}
if [ -z "$fixture_ip" ]; then
  sed '/^acl fixture /d; /^acl fixture_port /d; /^http_access allow fixture /d' /etc/squid/squid.conf > /tmp/squid.conf
  exec squid -N -f /tmp/squid.conf
fi
case "$fixture_ip" in
  ''|.*|*.|*..*|*[!0-9.]*) echo 'FIXTURE_IP must be an IPv4 address' >&2; exit 1 ;;
esac
old_ifs=$IFS
IFS=.
set -f
set -- $fixture_ip
IFS=$old_ifs
[ "$#" -eq 4 ] || { echo 'FIXTURE_IP must contain four octets' >&2; exit 1; }
for octet do
  case "$octet" in
    ''|0[0-9]*) echo 'FIXTURE_IP has an invalid octet' >&2; exit 1 ;;
  esac
  [ "${#octet}" -le 3 ] && [ "$octet" -le 255 ] || {
    echo 'FIXTURE_IP has an invalid octet' >&2
    exit 1
  }
done

sed "s/__FIXTURE_IP__/$fixture_ip/g" /etc/squid/squid.conf > /tmp/squid.conf
exec squid -N -f /tmp/squid.conf
