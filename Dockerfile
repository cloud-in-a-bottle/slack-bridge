# Dockerfile for openhost-slack-bridge.
#
# Single-process Python app: a tiny Flask responder that handles
# Slack slash commands and returns Jitsi room URLs (and, in the
# future, possibly other on-zone resource URLs).
#
# python:3.13-slim is the smallest image that still has a working
# pip + ssl out of the box.  The whole runtime including deps is
# under 60 MiB.
FROM python:3.13-slim

# -- system deps --------------------------------------------------
# Just curl for the health check (so OpenHost's `[routing]
# health_check = "/health"` GET inside the container can verify
# liveness without depending on an external probe).  Everything
# else is pure-Python.
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# -- python deps --------------------------------------------------
# Pinned for reproducibility; bump deliberately.  Flask 3 ships an
# acceptable WSGI shim out of the box; gunicorn fronts it for prod.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# -- application --------------------------------------------------
COPY app /app
WORKDIR /app

# OpenHost mounts the persistent app-data directory into the
# container at $OPENHOST_APP_DATA_DIR (set by compute_space at
# container start).  We read the operator-configured Slack
# signing secret from there at request time, so changing the
# secret only needs the operator to write the file — no rebuild.

# -- runtime ------------------------------------------------------
# gunicorn binds 0.0.0.0:8080 to match openhost.toml's port=8080.
# 1 worker is plenty: the responder is millisecond-fast and Slack
# tolerates a 3-second response budget, so we'd hit no concurrency
# wall before saturating any sane traffic.  Logs go to stdout so
# OpenHost's log pipeline picks them up.
EXPOSE 8080
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "main:app"]
