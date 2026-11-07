#!/bin/sh
set -eu
umask 077
browser_port=${BROWSER_PORT:-8766}
web_port=${WEB_PORT:-8765}
case "$web_port" in ''|*[!0-9]*) exit 1;; esac
[ "${#web_port}" -le 5 ] && [ "$web_port" -ge 1024 ] && [ "$web_port" -le 65535 ] || exit 1
case "$browser_port" in ''|*[!0-9]*) echo 'Invalid browser port' >&2; exit 1;; esac
[ "${#browser_port}" -le 5 ] && [ "$browser_port" -ge 1024 ] && [ "$browser_port" -le 65535 ] || exit 1
sed -e "s/__BROWSER_PORT__/$browser_port/g" -e "s/__WEB_PORT__/$web_port/g" /etc/kiwm/app.conf.template > /tmp/kiwm-sites.conf
if [ -n "${KIWM_LAN_IP:-}" ]; then
  lan_port=${KIWM_LAN_PORT:-8765}
  lan_subnet=${KIWM_LAN_SUBNET:?Allowed LAN subnet required}
  case "$KIWM_LAN_IP:$lan_subnet:$lan_port" in *[!0-9.:/]*) echo 'Invalid LAN configuration' >&2; exit 1;; esac
  printf '%s\n' "$KIWM_LAN_IP/32" "$lan_subnet" | awk -F '[./]' '
    NF!=5 {exit 1}
    {for(i=1;i<=4;i++) if($i<0 || $i>255 || $i!~/^[0-9]+$/) exit 1;
     if($5!~/^[0-9]+$/ || $5>32) exit 1;
     if(!(($1==10 && $5>=8) || ($1==172 && $2>=16 && $2<=31 && $5>=12) || ($1==192 && $2==168 && $5>=16))) exit 1;}' || exit 1
  [ "${#lan_port}" -le 5 ] && [ "$lan_port" -ge 1024 ] && [ "$lan_port" -le 65535 ] || exit 1
  sed -e "s#__LAN_IP__#$KIWM_LAN_IP#g" -e "s#__LAN_SUBNET__#$lan_subnet#g" \
      -e "s#__LAN_AUTHORITY__#$KIWM_LAN_IP:$lan_port#g" \
      -e "s#__LAN_URL__#http://$KIWM_LAN_IP:$lan_port#g" \
      -e 's#__ORIGIN_VARIABLE__#lan_origin_allowed#g' \
      /etc/kiwm/lan.conf.template >> /tmp/kiwm-sites.conf
fi
if [ -n "${KIWM_PUBLIC_URL:-}" ]; then
  [ -n "${KIWM_LAN_IP:-}" ] || { echo 'KIWM_PUBLIC_URL requires the LAN endpoint' >&2; exit 1; }
  case "$KIWM_PUBLIC_URL" in https://*) ;; *) echo 'Public URL requires HTTPS' >&2; exit 1;; esac
  public_host=${KIWM_PUBLIC_URL#https://}; public_host=${public_host%/}
  case "$public_host" in *[!0-9.]* ) ;; *) echo 'Public hostname required' >&2; exit 1;; esac
  case "$public_host" in *.local|*.lan|*.internal|*.localhost) echo 'Public hostname required' >&2; exit 1;; esac
  printf '%s\n' "$public_host" | awk -F '.' '
    length($0)>253 || NF<2 {exit 1}
    {for(i=1;i<=NF;i++) if(length($i)>63 || $i!~/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/) exit 1;}' || { echo 'Invalid public hostname' >&2; exit 1; }
  # Cloudflare overwrites X-Forwarded-Proto with the visitor's actual protocol.
  # Upgrade before serving a login form. Stale HTTP form posts restart on HTTPS
  # without forwarding the submitted body or its HTTP Origin.
  cat >> /tmp/kiwm-sites.conf <<'EOF'
map "$http_x_forwarded_proto|$request_method" $public_http_redirect {
  default 303;
  "~^https[|]" 0;
  "~[|](GET|HEAD)$" 308;
}
EOF
  sed -e "s#__LAN_IP__#$public_host#g" -e "s#__LAN_SUBNET__#$lan_subnet#g" \
      -e "s#__LAN_AUTHORITY__#$public_host#g" -e "s#__LAN_URL__#https://$public_host#g" \
      -e 's#__ORIGIN_VARIABLE__#public_origin_allowed#g' \
      -e 's#proxy_set_header X-Forwarded-Proto http;#proxy_set_header X-Forwarded-Proto https;#g' \
      /etc/kiwm/lan.conf.template | awk -v host="$public_host" '
        /if \(\$public_origin_allowed = 0\)/ {
          print "  if ($public_http_redirect = 308) { return 308 https://" host "$request_uri; }";
          print "  if ($public_http_redirect = 303) { return 303 https://" host "/login; }";
        }
        {print}
      ' >> /tmp/kiwm-sites.conf
fi
if [ "${KIWM_TEST_TOOLS:-0}" = 1 ]; then
  printf '\nserver { listen 8082; set $kiwm_mailpit http://mailpit:8025; location / { proxy_pass $kiwm_mailpit; } }\n' >> /tmp/kiwm-sites.conf
fi
exec nginx -c /etc/kiwm/nginx.conf -g 'daemon off;'
