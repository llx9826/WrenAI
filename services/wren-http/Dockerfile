FROM python:3.11-slim

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    WREN_HTTP_WORKSPACES_FILE=/srv/config/workspaces.json \
    WREN_HTTP_DUCKDB_ROOT=/srv/data \
    WREN_HTTP_WREN_HOME=/srv/config/wren-home \
    WREN_HTTP_HTTP_HOST=0.0.0.0 \
    WREN_HTTP_HTTP_PORT=8001

WORKDIR /src
COPY core/wren ./core/wren
COPY sdk/wren-pydantic ./sdk/wren-pydantic
COPY services/wren-http ./services/wren-http
RUN pip install --no-cache-dir -r ./services/wren-http/requirements-runtime.txt \
    && pip install --no-cache-dir --no-deps ./core/wren ./sdk/wren-pydantic ./services/wren-http \
    && useradd --uid 10001 --create-home wrenhttp \
    && mkdir -p /srv/config/wren-home /srv/data \
    && chown -R wrenhttp:wrenhttp /srv/config /srv/data

USER wrenhttp
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)"
CMD ["python", "-m", "wren_http"]
