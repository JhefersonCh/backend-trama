FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

FROM python:3.12.12-slim
COPY --from=uv /uv /usr/local/bin/uv

ENV MEDIA_DIR=/data/media UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

# WeasyPrint (report.pdf) needs pango; fonts-dejavu-core gives it a real font. ~60 MB; if the import fails at
# runtime the endpoint answers 501 and report.html keeps working.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 1000 --create-home trama \
    && mkdir -p /run/trama-secrets && chown trama:trama /run/trama-secrets && chmod 700 /run/trama-secrets \
    && mkdir -p /data/media && chown trama:trama /data/media

WORKDIR /srv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.12

COPY app ./app

USER trama
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
