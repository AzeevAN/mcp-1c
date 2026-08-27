# Node нужен только для получения статических файлов. В runtime ни Node, ни
# node_modules не переходят: React обслуживает тот же Python-процесс.
FROM node:22.13.1-bookworm-slim@sha256:83fdfa2a4de32d7f8d79829ea259bd6a4821f8b2d123204ac467fbe3966450fc AS dashboard-build

WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# Один контейнер без внешних баз. Всё живёт в памяти: две конфигурации — 85 МБ,
# справка платформы — 160 МБ. Сравнение: исходный шаблон тянул Qdrant с torch и
# cuda на 6+ ГБ образа, разобранный аналог — Elasticsearch на 2 ГБ памяти ради
# 32 МБ индекса.
FROM python:3.12-slim@sha256:590cad70271b6c1795c6a11fb5c110efca593adbd0d4883cd19c36df6a56467b AS runtime-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    MCP1C_DATA=/data

WORKDIR /app

COPY requirements-lock.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-lock.txt

COPY src/ ./src/

# Данные монтируются томом: выгрузки конфигураций и справка платформы —
# проприетарный контент, в образ он не попадает.
RUN mkdir -p /data/bootstrap /data/index /data/sources \
    && useradd --system --uid 10001 mcp1c \
    && chown -R mcp1c /data /app
USER mcp1c

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "mcp1c.server"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--data", "/data"]

# Опциональный образ с React-статикой. Он остаётся однопроцессным и читает
# Registry только через API того же MCP-сервера.
FROM runtime-base AS runtime-dashboard
COPY --from=dashboard-build --chown=10001:10001 /dashboard/dist /app/dashboard/dist
ENV MCP1C_DASHBOARD=spa \
    MCP1C_DASHBOARD_DIST=/app/dashboard/dist

# Финальный target по умолчанию сохраняет прежнее поведение и не заставляет
# обычную сборку выполнять Node-stage.
FROM runtime-base AS runtime-core
ENV MCP1C_DASHBOARD=classic
