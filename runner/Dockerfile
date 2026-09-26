FROM python:3.12-slim

# libgomp1: LightGBM's OpenMP runtime. Nothing else — no compilers, no git, no
# curl. Model-written code runs in this image, so every tool left out of it is
# one less thing that code can reach for.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=True
WORKDIR /app

COPY pyproject.toml README.md ./
RUN uv pip install --system --no-cache .

COPY runner ./runner

# Unprivileged. The sandbox creates its working directories under /tmp.
RUN useradd --create-home --uid 10001 runner
USER runner

EXPOSE 8080
CMD ["uvicorn", "runner.app:app", "--host", "0.0.0.0", "--port", "8080"]
