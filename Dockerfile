FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHROME_BIN=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium

# Headless browser deps for `nodriver`-based universal HTML loading.
# - `chromium` is used directly via CDP (no webdriver needed).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        ca-certificates \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package (PEP 517 build via hatchling).
COPY pyproject.toml README.md /app/
COPY src /app/src

RUN python -m pip install --upgrade pip \
    && python -m pip install .

# Run as non-root for better container security.
RUN useradd -m -u 10001 app \
    && chown -R app:app /app
USER app

# Default to stdio (for `docker run -i ...`). Override with `--http` for HTTP mode.
ENTRYPOINT ["mcp-web-search"]
CMD ["--stdio"]
