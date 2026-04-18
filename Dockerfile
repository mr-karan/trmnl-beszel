FROM python:3.13-alpine

RUN apk add --no-cache tzdata tini \
 && wget -qO /usr/local/bin/supercronic \
    https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 \
 && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY push.py /app/push.py
COPY crontab /app/crontab

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["supercronic", "-passthrough-logs", "/app/crontab"]
