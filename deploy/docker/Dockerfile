# --- Stage 1: builder ----------------------------------------------------
FROM python:3.11-slim AS builder

# uv for fast dependency installation
RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy both project pyproject files and resolve dependencies
COPY pyproject.toml uv.lock* ./
COPY services/oanda_mcp/pyproject.toml services/oanda_mcp/
COPY app/ ./app/
COPY pinned/ ./pinned/
COPY services/oanda_mcp/src/ services/oanda_mcp/src/

RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install --no-cache .

# --- Stage 2: runtime ----------------------------------------------------
FROM python:3.11-slim AS runtime

# Run as non-root
RUN useradd --create-home --shell /bin/bash nito
WORKDIR /home/nito

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/app ./app
COPY --from=builder /build/pinned ./pinned
COPY --from=builder /build/services ./services

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/home/nito
ENV PYTHONUNBUFFERED=1

USER nito
EXPOSE 8080

# Default: run the agent. The OANDA MCP subprocess is started by the
# agent process on demand (lazy).
CMD ["python", "-m", "app.main"]
