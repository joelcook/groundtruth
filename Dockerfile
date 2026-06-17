FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GT_MCP_HOST=0.0.0.0 \
    GT_MCP_PORT=8765

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The image carries the data layer + code and starts the MCP server.
COPY --chown=app:app db/groundtruth.db ./db/
COPY --chown=app:app db/schema.sql ./db/
COPY --chown=app:app sources ./sources
COPY --chown=app:app wiki ./wiki
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app evals ./evals
COPY --chown=app:app groundtruth ./groundtruth
COPY --chown=app:app scripts ./scripts

RUN mkdir -p /app/logs && chown -R app:app /app
USER app
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1',8765),3).close()"

# Set GT_MCP_API_KEY at runtime to require auth.
CMD ["python", "scripts/serve_mcp.py"]
