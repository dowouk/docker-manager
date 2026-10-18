FROM python:3.12-slim

# Docker CLI + Compose plugin (from Docker's own apt repository, for the Debian
# release this base image is built on), nsenter (util-linux) for running Mailcow's
# update.sh on the host, tini as a proper init, and tzdata for the TZ setting.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tini tzdata util-linux; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc; \
    chmod a+r /etc/apt/keyrings/docker.asc; \
    . /etc/os-release; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

# Install as a normal (non-editable) package, then drop the source so only the
# installed copy (with its templates and static files) is used.
COPY pyproject.toml /src/
COPY app /src/app
RUN pip install --no-cache-dir /src && rm -rf /src

WORKDIR /app

# DATA_DIR is not set here: Settings already defaults to /data, and anything set
# with ENV would be one more variable to keep out of the compose child processes.
ENV PYTHONUNBUFFERED=1 \
    TINI_SUBREAPER=1
VOLUME /data
EXPOSE 8000

# Runs as root on purpose: the dashboard talks to the mounted Docker socket and
# (on servers that run Mailcow) enters the host's namespaces with nsenter. Both need
# root; socket access is root-equivalent on the host whatever user runs here.
#
# tini reaps processes left behind by docker compose / update.sh. TINI_SUBREAPER
# keeps that working on servers that run Mailcow, where `pid: host` means tini isn't PID 1.
# The self-update helper container sets its own entrypoint, so it isn't affected.
# --timeout-graceful-shutdown keeps open live-log streams from delaying a stop
# (a running job is left as "running" and classified on the next start).
# Liveness only (/healthz is public and checks the database). The self-update helper
# runs the same image with its healthcheck disabled.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "--factory", "app.main:build_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
