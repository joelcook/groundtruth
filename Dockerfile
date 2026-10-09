# The MCP server, with your project baked in.
#
#   docker build -t my-assistant --build-arg PROJECT=my-assistant .
#   docker run -p 8765:8765 -e GT_MCP_API_KEY=... my-assistant
#
# The build runs ./gt check, so an image with broken facts never gets made.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GT_MCP_HOST=0.0.0.0 \
    GT_MCP_PORT=8765 \
    GT_PROJECT=/app/project

ARG PROJECT=examples/library

WORKDIR /app
RUN useradd --system --create-home app

COPY pyproject.toml README.md ./
COPY groundtruth ./groundtruth
RUN pip install --no-cache-dir .

COPY ${PROJECT} /app/project
RUN python -m groundtruth build && python -m groundtruth check && chown -R app:app /app

USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200 else 1)"

# Set GT_MCP_API_KEY at runtime before exposing this anywhere.
CMD ["python", "-m", "groundtruth", "serve"]
