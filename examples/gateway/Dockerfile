FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# uv only exports the lock: pip installs every dependency, and the build backend, by hash.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM base AS build
COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY rampart ./rampart
RUN uv export --frozen --no-emit-project -o requirements.txt \
 && uv export --frozen --no-emit-project --only-group build -o build.txt \
 && pip install --require-hashes --no-deps -r build.txt \
 && pip wheel --wheel-dir /wheels --require-hashes --no-deps -r requirements.txt \
 && pip wheel --wheel-dir /wheels --no-deps --no-build-isolation .

FROM base AS runtime
RUN useradd --create-home --uid 10001 rampart
COPY --from=build /wheels /wheels
RUN pip install --no-index --no-deps /wheels/*.whl && rm -rf /wheels
# No principals file: API keys and who holds them are mounted at run time (docker-compose.yml),
# never baked into an image.
COPY samples ./samples
USER rampart
# 8080 is the API; 9464 serves Prometheus metrics, for the internal network only.
EXPOSE 8080 9464
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["rampart", "serve", "--host", "0.0.0.0", "--port", "8080", "--metrics-host", "0.0.0.0"]

FROM base AS test
COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY rampart ./rampart
RUN uv export --frozen --no-emit-project --extra dev --group build -o requirements-dev.txt \
 && pip install --require-hashes --no-deps -r requirements-dev.txt \
 && pip install --no-deps --no-build-isolation -e .
COPY . .
CMD ["pytest", "-q"]
