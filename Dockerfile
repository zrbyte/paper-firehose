# Paper Firehose container image
# Default: install released package from PyPI and run the CLI

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Persist app state/config outside the image
    PAPER_FIREHOSE_DATA_DIR=/data

WORKDIR /app

# OS deps: common build tools and CA certs for HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       ca-certificates \
       curl \
       git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip first for better wheels
RUN python -m pip install --upgrade pip

# ---- Install app ----
# Mode 1 (default): install from PyPI
ARG INSTALL_SRC=pypi
ARG PACKAGE_NAME=paper_firehose

# You can pass a specific version at build: --build-arg PACKAGE_SPEC="paper_firehose==0.1.0"
ARG PACKAGE_SPEC

RUN if [ "$INSTALL_SRC" = "pypi" ]; then \
        python -m pip install --no-cache-dir ${PACKAGE_SPEC:-$PACKAGE_NAME}; \
    fi

# Mode 2 (dev): build from local source (copy only necessary files)
# To use: docker build --build-arg INSTALL_SRC=local .
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN if [ "$INSTALL_SRC" = "local" ]; then \
        python -m pip install --no-cache-dir .; \
    fi

# Prepare runtime data dir and drop privileges
RUN mkdir -p /data \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /data

USER appuser
VOLUME ["/data"]

# Expose no network port by default (CLI app). If you later add a web UI, expose it here.

# Default entry runs the CLI. Additional args are subcommands, e.g. `filter`, `rank`, ...
ENTRYPOINT ["paper-firehose"]
CMD ["status"]

