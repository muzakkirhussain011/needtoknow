FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY rampart ./rampart
RUN pip wheel --wheel-dir /wheels .

FROM base AS runtime
RUN useradd --create-home --uid 10001 rampart
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels
COPY config ./config
COPY samples ./samples
USER rampart
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["rampart", "serve", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS test
COPY pyproject.toml README.md ./
COPY rampart ./rampart
RUN pip install -e ".[dev]"
COPY . .
CMD ["pytest", "-q"]
