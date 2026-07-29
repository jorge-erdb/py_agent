FROM python:3.13-slim

# The agent's shell tool whitelists these binaries (see agent/tools.py).
# python:*-slim ships coreutils/grep/sed/findutils/diffutils but not these,
# so install them or those whitelisted commands fail at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        ripgrep \
        tree \
        file \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so the dependency layer caches across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user. Anything the LLM executes inherits this uid,
# so it cannot write to /app (owned by root) or to any read-only host mount.
RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data \
    && chown agent:agent /app/data
USER agent

EXPOSE 8000

# 0.0.0.0 is correct *inside* the container — the compose file is what
# restricts the published port to the host loopback.
ENV SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
