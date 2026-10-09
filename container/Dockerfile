# styx-trigger: watch a UPS through NUT and trigger styx on low battery.
#
# Build from the repository root:
#   docker build -f container/Dockerfile -t styx-trigger .

FROM alpine:3.22

RUN apk add --no-cache bash nut openssh-client tini

COPY scripts/trigger.sh /usr/local/bin/trigger.sh
COPY container/watch.sh container/entrypoint.sh /usr/local/bin/

ENTRYPOINT ["/sbin/tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
