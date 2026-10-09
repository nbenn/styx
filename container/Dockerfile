# styx-trigger: watch a UPS through NUT and trigger styx on low battery.
#
# Build from the repository root:
#   docker build -f container/Dockerfile -t styx-trigger .

FROM alpine:3.22

RUN apk add --no-cache bash nut openssh-client tini

COPY scripts/trigger.sh /usr/local/bin/trigger.sh
COPY container/common.sh container/entrypoint.sh container/watch.sh \
     container/check.sh container/simulate.sh /usr/local/lib/styx-trigger/
# The styx version this image belongs to, compared against the nodes by check
COPY styx/__init__.py /tmp/styx_init.py
RUN sed -n "s/^__version__ = '\(.*\)'$/\1/p" /tmp/styx_init.py \
        > /usr/local/lib/styx-trigger/VERSION \
 && rm /tmp/styx_init.py \
 && ln -s /usr/local/lib/styx-trigger/check.sh /usr/local/bin/check \
 && ln -s /usr/local/lib/styx-trigger/simulate.sh /usr/local/bin/simulate

ENTRYPOINT ["/sbin/tini", "-g", "--", "/usr/local/lib/styx-trigger/entrypoint.sh"]
