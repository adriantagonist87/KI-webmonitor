FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends squid ca-certificates && rm -rf /var/lib/apt/lists/*
# Public image assets must not inherit private checkout permissions.
COPY --chmod=0644 squid.conf /etc/squid/squid.conf
COPY --chmod=0755 start-proxy.sh /usr/local/bin/start-proxy
COPY --chmod=0755 check-proxy.sh /usr/local/bin/check-proxy
USER proxy
HEALTHCHECK --interval=15s --timeout=8s --start-period=15s --retries=5 CMD ["/usr/local/bin/check-proxy"]
CMD ["/usr/local/bin/start-proxy"]
