FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ARG SOURCE_REVISION=unreleased
LABEL org.opencontainers.image.source="https://github.com/OmerHanan1/buna-screening-tool" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.revision=$SOURCE_REVISION
COPY pyproject.toml /app/
COPY LICENSE NOTICE README.public.md /app/
COPY buna /app/buna
COPY frontend/src/theme.css /app/frontend/src/theme.css
COPY deploy/smoke_public.py /app/deploy/smoke_public.py
COPY release-context/wheels /wheels
RUN pip install --no-index --find-links=/wheels setuptools wheel \
    && pip install --no-index --find-links=/wheels --no-build-isolation . \
    && rm -rf /wheels
COPY release-context/hosted-corpus /corpus
RUN chmod -R a-w /corpus && mkdir -p /runtime && chmod 711 /runtime
ENV BUNA_PUBLIC_CORPUS=/corpus PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "buna.public_app:create_public_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
