FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg && curl -fsSLo /etc/apt/sources.list.d/brave-browser-release.sources https://brave-browser-apt-release.s3.brave.com/brave-browser.sources && apt-get update && apt-get install -y --no-install-recommends brave-browser xvfb openbox x11vnc novnc websockify socat fonts-liberation fonts-noto-color-emoji python3 && rm -rf /var/lib/apt/lists/* && useradd -u 1000 -m monitor
RUN apt-get update && apt-get install -y --no-install-recommends python3-websocket && rm -rf /var/lib/apt/lists/*
# Public image assets must remain readable after a checkout with umask 077.
COPY --chmod=0644 policies.json /etc/brave/policies/managed/monitor.json
COPY --chmod=0755 start.sh /usr/local/bin/start-browser
COPY --chmod=0644 close-browser.py /usr/local/bin/close-browser.py
# COPY's file mode can also affect newly created parent directories.
RUN chmod 0755 /etc/brave /etc/brave/policies /etc/brave/policies/managed
COPY --chmod=0644 healthcheck.py /usr/local/bin/healthcheck.py
RUN mkdir -p /data && chown 1000:1000 /data && chmod 0700 /data
ENV DISPLAY=:99 HOME=/data/home
USER 1000:1000
HEALTHCHECK --interval=15s --timeout=8s --start-period=30s --retries=5 CMD ["python3", "/usr/local/bin/healthcheck.py"]
CMD ["/usr/local/bin/start-browser"]
