# bricksentry MCP server in a container, running as a non-root user.
#
# Build from the repository root:
#   docker build -f deploy/container/Dockerfile -t bricksentry-mcp .
#
# stdio (for Claude, Cursor and other MCP clients), config mounted read-only:
#   docker run -i --rm -v "$PWD/bricksentry.yml:/config/bricksentry.yml:ro" \
#     -e DATABRICKS_HOST -e DATABRICKS_TOKEN bricksentry-mcp
#
# HTTP, published on this machine only (inside the container the server must listen on 0.0.0.0):
#   docker run --rm -p 127.0.0.1:8000:8000 -e BRICKSENTRY_HOST=0.0.0.0 \
#     -v "$PWD/bricksentry.yml:/config/bricksentry.yml:ro" -e DATABRICKS_HOST -e DATABRICKS_TOKEN \
#     bricksentry-mcp --config /config/bricksentry.yml --transport streamable-http --read-only

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install "/src[databricks,mcp]" && rm -rf /src \
    && useradd --create-home --uid 10001 bricksentry

USER bricksentry
WORKDIR /home/bricksentry
ENV BRICKSENTRY_STORE=/home/bricksentry/.bricksentry/assessments

ENTRYPOINT ["bricksentry", "mcp"]
CMD ["--config", "/config/bricksentry.yml"]
