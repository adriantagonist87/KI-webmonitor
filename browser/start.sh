#!/bin/sh
set -eu
umask 077
mkdir -p /data/home /data/profile /data/downloads /tmp/runtime
export XDG_RUNTIME_DIR=/tmp/runtime
rm -f /data/profile/SingletonLock /data/profile/SingletonCookie /data/profile/SingletonSocket
Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &
sleep 1
openbox > /tmp/openbox.log 2>&1 &
x11vnc -display :99 -localhost -nopw -forever -shared -rfbport 5900 -quiet > /tmp/vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 > /tmp/websockify.log 2>&1 &
socat TCP-LISTEN:9223,fork,bind=0.0.0.0 TCP:127.0.0.1:9222 &
brave-browser --no-sandbox --user-data-dir=/data/profile --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --remote-allow-origins=http://browser:9223,http://localhost:9222 --no-first-run --no-default-browser-check --disable-sync --disable-breakpad --disable-session-crashed-bubble --password-store=basic --proxy-server=http://proxy:3128 '--proxy-bypass-list=<-loopback>' --disable-quic --disable-features=DnsOverHttps,AsyncDns --window-size=1440,1000 about:blank > /tmp/chromium.log 2>&1 &
browser_pid=$!
shutdown() {
  python3 /usr/local/bin/close-browser.py
  count=0
  while kill -0 "$browser_pid" 2>/dev/null && [ "$count" -lt 40 ]; do
    sleep 0.25
    count=$((count + 1))
  done
  kill -TERM "$browser_pid" 2>/dev/null || true
  wait "$browser_pid" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT
wait "$browser_pid"
