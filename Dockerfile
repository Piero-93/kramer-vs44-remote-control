# syntax=docker/dockerfile:1
#
# kramer-vs44-remote-control - the HTTP API and web UI, as a service.
# Copyright (C) 2026 Piero Biagini. Licensed under the GNU GPL v3 or later.
#
# There is no install step in this file, and that is not an omission: the service
# is built on the Python standard library alone, so there is nothing to fetch, no
# wheel to compile and no lock file to keep honest.
#
#   docker build -t kramer-vs44 .
#   docker run -p 8000:8000 -e KRAMER_MATRIX=192.168.1.39 kramer-vs44
#
# See docker-compose.yml for the form meant to be pasted into TrueNAS Scale.

# slim rather than alpine, for a reason specific to this project. With no
# third-party packages there are no wheels to build, which removes the usual
# argument *for* alpine and leaves only the cost of a different C library. The
# minor version is pinned and the patch floats, so a rebuild picks up security
# updates.
#
# slim also cannot run Tkinter, which turns "the service must not import the GUI"
# from a convention into something the image enforces. To be exact, because the
# obvious phrasing is wrong: the tkinter package and the _tkinter extension are
# both present; what is missing is the Tcl/Tk shared libraries, so the import
# fails with "libtk8.6.so: cannot open shared object file". That is why the CI
# check performs a real import rather than asking importlib whether the module
# can be found - find_spec answers yes.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="kramer-vs44-remote-control" \
      org.opencontainers.image.description="HTTP API and web UI for Kramer VS-44HN HDMI matrices" \
      org.opencontainers.image.source="https://github.com/Piero-93/kramer-vs44-remote-control" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV KRAMER_IN_CONTAINER=1 \
    KRAMER_CONFIG=/config/kramer_gui_config.json \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A fixed non-root uid. /app is owned by root and only ever read; /config is the
# one thing this service writes, so its owner has to match the uid below - see
# the note about ownership in docker-compose.yml.
RUN groupadd --gid 1000 kramer \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /app \
            --shell /usr/sbin/nologin kramer \
 && mkdir -p /config \
 && chown kramer:kramer /config

WORKDIR /app

# Named individually, never "COPY . .", and paired with a deny-all
# .dockerignore. The reason is sharper than tidiness: a kramer_gui_config.json
# copied in here would sit next to the program and therefore *beat* the mounted
# /config volume, because a settings file beside the program wins. The image must
# contain no settings file at all.
COPY kramer_vs44.py kramer_server.py kramer_paths.py ./
COPY web/ ./web/

USER kramer
EXPOSE 8000

# /api/state answers 200 even when the matrix is unreachable, and 401 when a
# token is set. Both prove the HTTP layer is alive, which is all a healthcheck
# can honestly assert here: a switched-off matrix must not paint the app red.
# http.client rather than urllib because it returns 401 as a response instead of
# raising, and there is no curl in this image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import http.client,os,sys; c=http.client.HTTPConnection('127.0.0.1', int(os.environ.get('KRAMER_PORT','8000')), timeout=4); c.request('GET','/api/state'); sys.exit(0 if c.getresponse().status < 500 else 1)"

# Exec form, so this process is PID 1 and receives SIGTERM directly - which the
# service installs a handler for. Without that handler a stop would skip the
# cleanup, leave the matrix believing the connection is still open, and cost
# about 90 seconds before the restarted container could reconnect.
ENTRYPOINT ["python", "-u", "kramer_server.py"]
