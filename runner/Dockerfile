FROM python:3.12-slim

# libgomp1: LightGBM's OpenMP runtime. Nothing else — no compilers, no git, no
# curl. Model-written code runs in this image, so every tool left out of it is
# one less thing that code can reach for.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=True
WORKDIR /app

# Exactly the locked, hash-verified versions (runner/uv.lock). The runner
# package itself is copied below and imported from /app, not installed.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-emit-project -o /tmp/requirements.txt > /dev/null \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY runner ./runner

# Unprivileged. The sandbox creates its working directories under /tmp.
RUN useradd --create-home --uid 10001 runner
USER runner

EXPOSE 8080
CMD ["uvicorn", "runner.app:app", "--host", "0.0.0.0", "--port", "8080"]
