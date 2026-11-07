FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY --chmod=0644 nginx.conf /etc/kiwm/nginx.conf
COPY --chmod=0644 app.conf.template /etc/kiwm/app.conf.template
COPY --chmod=0644 lan.conf.template /etc/kiwm/lan.conf.template
COPY --chmod=0755 start.sh /usr/local/bin/start-ingress
USER root
RUN chmod 0755 /etc/kiwm
USER 101:101
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/usr/local/bin/start-ingress"]
