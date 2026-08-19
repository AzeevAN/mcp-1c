# Один контейнер без внешних баз. Всё живёт в памяти: две конфигурации — 85 МБ,
# справка платформы — 160 МБ. Сравнение: исходный шаблон тянул Qdrant с torch и
# cuda на 6+ ГБ образа, разобранный аналог — Elasticsearch на 2 ГБ памяти ради
# 32 МБ индекса.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    MCP1C_DATA=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Данные монтируются томом: выгрузки конфигураций и справка платформы —
# проприетарный контент, в образ он не попадает.
RUN mkdir -p /data/bootstrap /data/index /data/sources \
    && useradd --system --uid 10001 mcp1c \
    && chown -R mcp1c /data /app
USER mcp1c

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "mcp1c.server"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--data", "/data"]
