# The MCP server, with your project baked in.
#
#   docker build -t my-assistant --build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD) .
#   docker run -p 8765:8765 -e GT_MCP_API_KEY=... my-assistant
#
# It builds the project ./gt points at (groundtruth.project, set by ./gt init or
# ./gt use); --build-arg PROJECT=folder picks another. Building the demo needs
# --build-arg ALLOW_DEMO=1, so nobody ships the library example by accident.
# The build runs ./gt check --production, so an image with broken facts (or leftover
# template examples) never gets made. GT_BUILD_COMMIT shows up in /healthz, so
# ./gt watchdog --repo can tell when the running code falls behind main.
FROM python:3.12-slim

ARG PROJECT=""
ARG ALLOW_DEMO=0
ARG GT_BUILD_COMMIT=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GT_MCP_HOST=0.0.0.0 \
    GT_MCP_PORT=8765 \
    GT_PROJECT=/app/project \
    GT_BUILD_COMMIT=${GT_BUILD_COMMIT}

WORKDIR /app
RUN useradd --system --create-home app

COPY pyproject.toml README.md ./
COPY groundtruth ./groundtruth
RUN pip install --no-cache-dir .

COPY . /tmp/context
RUN set -e; P="${PROJECT:-$(cat /tmp/context/groundtruth.project 2>/dev/null || true)}"; P="${P:-examples/library}"; \
    if [ "$P" = "examples/library" ] && [ "$ALLOW_DEMO" != "1" ]; then \
      echo "This would build the library demo. Run ./gt init (or ./gt use) to point at your project," \
           "pass --build-arg PROJECT=folder, or --build-arg ALLOW_DEMO=1 to build the demo on purpose."; exit 1; fi; \
    test -f "/tmp/context/$P/groundtruth.yaml" || { echo "no project at $P"; exit 1; }; \
    cp -r "/tmp/context/$P" /app/project && rm -rf /tmp/context
RUN python -m groundtruth build && python -m groundtruth check --production && chown -R app:app /app

USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200 else 1)"

# Set GT_MCP_API_KEY at runtime before exposing this anywhere.
CMD ["python", "-m", "groundtruth", "serve"]
