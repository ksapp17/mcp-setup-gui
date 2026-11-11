# Remote MCP server for one project.
#
#   docker build --build-arg PROJECT=project.json -t mcp-weather .
#   docker run --rm -p 8000:8000 -e MCP_BEARER_TOKEN -e MCP_PUBLIC_URL -e MCP_SECRET_WEATHER_OPENWEATHER_APIKEY mcp-weather
#
# `uv run remote.py env --project project.json` lists the exact variables a project needs.
# No secret is baked into the image: the token and API keys arrive as environment variables.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so editing project.json doesn't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY runtime.py oauth.py ./
ARG PROJECT=project.json
COPY ${PROJECT} ./project.json

# Don't run as root
RUN useradd --create-home --uid 10001 mcp
USER mcp

# Most hosts set PORT; the runtime reads it
ENV PORT=8000
EXPOSE 8000

CMD ["/app/.venv/bin/python", "runtime.py", "--project", "/app/project.json", "--http", "--host", "0.0.0.0"]
